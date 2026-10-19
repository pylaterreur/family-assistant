"""Resolve script inputs and describe invocation-local execution authority."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypedDict

from family_assistant.scripting.apis.keychute import (
    get_keychute_config,
    keychute_external_function_names,
)
from family_assistant.scripting.validator import ScriptValidator
from family_assistant.security.definition_records import (
    DefinitionResolution,
    resolve_definition_record,
    script_definition_content,
)
from family_assistant.security.script_closure import (
    ScriptBinding,
    ScriptClosureError,
    resolve_script_closure,
)
from family_assistant.security.taint import (
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_state_into_tracker,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from family_assistant.security.script_closure import ScriptArtifact, ScriptClosure
    from family_assistant.storage.repositories.scripts import ScriptRow
    from family_assistant.tools.types import ToolDefinition, ToolExecutionContext


class ScriptInvocationArguments(TypedDict, total=False):
    """Public script invocation inputs before source resolution."""

    script: str | None
    globals: dict[str, object] | None
    name: str | None
    parameters: dict[str, object] | None
    script_bindings: list[dict[str, object]] | None


class ScriptPreparationError(ValueError):
    """An invalid invocation, rendered identically by direct and gated callers."""

    def __init__(self, message: str, error_type: str = "validation_error") -> None:
        super().__init__(message)
        self.error_type = error_type


@dataclass(frozen=True)
class ScriptReviewContext:
    """Complete program evidence, never trusted reviewer instructions."""

    source: str
    inputs: dict[str, object]
    tools: tuple[ToolDefinition, ...]
    external_functions: tuple[str, ...]
    stored_name: str | None = None
    definition: DefinitionResolution | None = None
    script_bindings: tuple[dict[str, object], ...] = ()
    policy: dict[str, object] = field(default_factory=dict)
    decision: str = "unreviewed"
    review_id: str | None = None
    approval_active: bool = False


def _copy_globals(values: dict[str, object]) -> dict[str, object]:
    """Pin data inputs while preserving Python callers' external function bindings."""
    return {
        key: value if callable(value) else copy.deepcopy(value)
        for key, value in values.items()
    }


@dataclass
class PreparedScriptInvocation:
    """Pinned execution payload and the review evidence that describes it."""

    review: ScriptReviewContext
    globals: dict[str, object]
    approved: bool = False
    forecast_approval: bool = False
    """Approved only as observe mode's forecast of a confirmation no human gave.

    Such an approval stands in for the human ``enforce`` would have asked, so
    it binds only gates whose verdicts are themselves forecasts; every live
    gate still decides on its own.
    """
    bound_child: bool = False
    watched_tools: frozenset[str] = frozenset()
    """Tools whose every call the approving review asked to see on its own."""

    def reachable_capabilities(self) -> frozenset[str]:
        """Tools and external functions the program can call, conservatively.

        Any name the source or a bound child mentions counts, called or not,
        and a program that dispatches tools by name can reach every tool it
        was offered.
        """
        sources = (
            self.review.source,
            *(
                str(binding.get("script_code", ""))
                for binding in self.review.script_bindings
            ),
        )
        tools = {
            str(tool["function"]["name"])
            for tool in self.review.tools
            if isinstance(tool.get("function"), dict)
        }
        referenced = _referenced_names(sources)
        if referenced is None:
            return frozenset(tools | set(self.review.external_functions))
        reachable = tools if referenced & DYNAMIC_TOOL_FUNCTIONS else referenced & tools
        return frozenset(reachable | (referenced & set(self.review.external_functions)))

    def arguments(self) -> dict[str, object]:
        """Return the inline payload used for review, confirmation and execution."""
        arguments: dict[str, object] = {
            "script": self.review.source,
            "globals": _copy_globals(self.globals),
        }
        if self.review.script_bindings:
            arguments["script_bindings"] = copy.deepcopy(
                list(self.review.script_bindings)
            )
        return arguments


@dataclass
class ScriptExecutionScope:
    """Shared only by operations within one running program, not sibling calls."""

    invocation: PreparedScriptInvocation
    parent: ScriptExecutionScope | None = None
    active: bool = True

    @property
    def approved(self) -> bool:
        if not self.active:
            return False
        if self.invocation.approved:
            return True
        return self._bound_parent is not None and self._bound_parent.approved

    @property
    def approving_review_id(self) -> str | None:
        """The review whose allow this program's operations run under."""
        if self.invocation.approved or self._bound_parent is None:
            return self.invocation.review.review_id
        return self._bound_parent.approving_review_id

    @property
    def approval_is_forecast(self) -> bool:
        """Whether this run's approval only forecasts a human confirmation."""
        if self.invocation.approved:
            return self.invocation.forecast_approval
        return (
            self._bound_parent is not None and self._bound_parent.approval_is_forecast
        )

    @property
    def watched_tools(self) -> frozenset[str]:
        """Tools this run's calls to never inherit approval for.

        A bound child is part of the program that names it, so the program's
        watch covers it, whichever review approved it.
        """
        bound = (
            self._bound_parent.watched_tools
            if self._bound_parent is not None
            else frozenset()
        )
        return self.invocation.watched_tools | bound

    @property
    def _bound_parent(self) -> ScriptExecutionScope | None:
        """The program this one is statically bound into, and so part of."""
        return self.parent if self.invocation.bound_child else None

    def review_contexts(self) -> tuple[ScriptReviewContext, ...]:
        """Keep enclosing source available even after its approval has ended."""
        parents = self.parent.review_contexts() if self.parent is not None else ()
        return (
            *parents,
            replace(
                self.invocation.review,
                approval_active=self.approved and not self.approval_is_forecast,
            ),
        )


DYNAMIC_TOOL_FUNCTIONS = frozenset({"tools_execute", "tools_execute_json"})
"""Script functions that call a tool chosen by name at runtime."""


def _referenced_names(sources: Iterable[str]) -> frozenset[str] | None:
    """Every bare name the sources mention, or ``None`` if one does not parse."""
    names: set[str] = set()
    for source in sources:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return None
        names.update(node.id for node in ast.walk(tree) if isinstance(node, ast.Name))
    return frozenset(names)


async def _resolve_bound_closure(
    context: ToolExecutionContext,
    source: str,
    row: ScriptRow | None,
    supplied_bindings: list[dict[str, object]] | None,
    bound_child: bool,
) -> ScriptClosure:
    def record_artifact_taint(artifact: ScriptArtifact) -> None:
        if row is None or artifact.script.name != row.name:
            _record_definition_taint(context, artifact.resolution, artifact.script.name)

    closure = await resolve_script_closure(
        context.db_context, source, loaded_root=row, on_artifact=record_artifact_taint
    )
    if supplied_bindings is not None:
        closure.verify_bindings(supplied_bindings)
    if bound_child and context.script_execution is not None:
        inherited_bindings = {
            item["name"]: item
            for item in context.script_execution.invocation.review.script_bindings
        }
        for binding in closure.bindings:
            if inherited_bindings.get(binding.name) != binding.to_dict():
                raise ScriptClosureError(
                    f"Stored script '{binding.name}' changed since preparation; prepare and review again"
                )
    return closure


def _record_definition_taint(
    context: ToolExecutionContext,
    definition: DefinitionResolution | None,
    stored_name: str | None,
) -> None:
    """Import loaded definition provenance before its content can reach a result."""
    if definition is not None and context.taint_tracker is not None:
        if definition.taint_metadata is not None:
            # Stored authorship is provenance, not a live profile authorization.
            definition_state = replace(
                TurnTaintState.from_metadata(definition.taint_metadata),
                approved_sinks=frozenset(),
            )
            merge_taint_state_into_tracker(
                context.taint_tracker,
                definition_state,
            )
        context.taint_tracker.add_source(
            TaintSource(
                source_type=TaintSourceType.TOOL_OUTPUT,
                source_id=f"script:{stored_name}",
                tier=definition.tier,
                labels=frozenset({"script_definition"}),
                reason="Provenance of the exact loaded script definition.",
            )
        )
        context.taint_policy_snapshot = None


async def prepare_script_invocation(
    context: ToolExecutionContext,
    script: str | None = None,
    globals: dict[str, object] | None = None,
    name: str | None = None,
    parameters: dict[str, object] | None = None,
    *,
    script_bindings: list[dict[str, object]] | None = None,
    allow_external_script_apis: bool = True,
) -> PreparedScriptInvocation:
    """Resolve and validate without evaluating code or constructing external APIs."""
    if name and script:
        raise ScriptPreparationError(
            "Provide either 'script' (inline) or 'name' (stored), not both"
        )
    inputs = _copy_globals(globals or {})
    definition = None
    row = None
    bound_child = False
    if name and not script:
        row = await context.db_context.scripts.get_by_name(name)
        if row is None:
            raise ScriptPreparationError(f"Script '{name}' not found", "not_found")
        definition = resolve_definition_record(
            row.definition_record,
            script_definition_content(
                name=row.name,
                description=row.description,
                script_code=row.script_code,
                parameters_schema=row.parameters_schema,
            ),
        )
        _record_definition_taint(context, definition, name)
        parent = context.script_execution
        if parent is not None:
            expected = next(
                (
                    binding
                    for binding in parent.invocation.review.script_bindings
                    if binding["name"] == name
                ),
                None,
            )
            if expected is not None:
                if ScriptBinding.from_row(row).to_dict() != expected:
                    raise ScriptPreparationError(
                        f"Stored script '{name}' changed since preparation; prepare and review again",
                        "stale_script_binding",
                    )
                bound_child = True
        script = row.script_code
        if row.parameters_schema:
            required = row.parameters_schema.get("required", [])
            if isinstance(required, list):
                for key in required:
                    if key not in (parameters or {}):
                        raise ScriptPreparationError(
                            f"Missing required parameter: {key}"
                        )
        inputs.update(_copy_globals(parameters or {}))
    if not script:
        raise ScriptPreparationError(
            "Either 'script' (inline code) or 'name' (stored script) must be provided"
        )

    provider = context.tools_provider
    if provider is None and context.processing_service is not None:
        provider = context.processing_service.tools_provider
    definitions = await provider.get_tool_definitions() if provider is not None else []
    external = [key for key, value in inputs.items() if callable(value)]
    if allow_external_script_apis:
        external.extend(
            keychute_external_function_names(get_keychute_config(context)) or []
        )

    validation = ScriptValidator(tool_definitions=definitions).validate(
        script,
        input_names=[key for key, value in inputs.items() if not callable(value)],
        extra_external_functions=external,
        include_tools_api=provider is not None,
        include_attachment_api=bool(context.attachment_registry),
    )
    if not validation.is_valid:
        first = validation.errors[0] if validation.errors else None
        if first and first.message.startswith("Syntax error"):
            location = f" at line {first.line}" if first.line else ""
            raise ScriptPreparationError(
                f"Syntax error in script{location}: {first.message}", "syntax_error"
            )
        raise ScriptPreparationError(
            f"Script validation failed: {validation.error_message}"
        )
    try:
        closure = await _resolve_bound_closure(
            context, script, row, script_bindings, bound_child
        )
    except ScriptClosureError as exc:
        raise ScriptPreparationError(str(exc), "stale_script_binding") from exc
    if closure.resolution is not None:
        definition = (
            closure.resolution
            if definition is None
            else definition.combine(closure.resolution)
        )
    return PreparedScriptInvocation(
        review=ScriptReviewContext(
            source=script,
            inputs=_copy_globals(inputs),
            tools=tuple(copy.deepcopy(definitions)),
            external_functions=tuple([
                *external,
                "llm",
                "llm_json",
                "classify",
                "classify_yes_no",
                "wake_llm",
                "json_*",
                "time_*",
                "base64_*",
                *(["attachment_*"] if context.attachment_registry else []),
            ]),
            stored_name=name,
            definition=definition,
            script_bindings=tuple(binding.to_dict() for binding in closure.bindings),
        ),
        globals=inputs,
        bound_child=bound_child,
    )
