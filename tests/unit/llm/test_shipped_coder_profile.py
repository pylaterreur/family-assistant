"""The shipped `coder` profile must reach the managed agent, not a chat model.

Three things can break silently between `defaults.yaml` and an actual agent
run: the profile could name a model that routes to `generateContent` instead of
the Interactions API, a tier's model could fail to reach the agent (leaving it
on whatever model the API defaults to), or a fallback could quietly answer from
a chat model's own knowledge instead of running the task. Each is a
plausible-looking answer rather than an error, so each is pinned here.
"""

from zoneinfo import ZoneInfo

import pytest

from family_assistant.assistant import Assistant, validate_antigravity_agent_config
from family_assistant.config_models import (
    AppConfig,
    ProcessingConfig,
    RetryConfig,
    RetryModelConfig,
    ToolsConfig,
)
from family_assistant.delegation_security import DelegationSecurityLevel
from family_assistant.llm.messages import SystemMessage, UserMessage
from family_assistant.llm.model_tiers import validate_profile_model_tier
from family_assistant.llm.providers.google_genai_client import (
    GoogleGenAIClient,
    is_interactions_agent_model,
)
from family_assistant.processing import ProcessingServiceConfig
from family_assistant.processing.interactions_agent_service import (
    InteractionsAgentProcessingService,
)
from family_assistant.security.taint import (
    SinkClass,
    SourceTrustTier,
    TaintPolicyConfig,
    TaintPolicyEvaluator,
    TaintPolicyMode,
    TaintPolicyOutcome,
    TaintSource,
    TaintSourceType,
    TurnTaintState,
    merge_taint_policy_config,
)
from family_assistant.tools import LocalToolsProvider
from tests.unit.conftest import shipped_profile


def test_shipped_coder_profile_runs_the_agent_on_flash_by_default(
    shipped_config: AppConfig,
) -> None:
    """The profile names the managed agent, and its default tier is Flash."""
    profile = shipped_profile(shipped_config, "coder")
    processing_config = profile.processing_config

    assert processing_config.antigravity_config is not None
    assert is_interactions_agent_model(processing_config.antigravity_config.agent)
    assert processing_config.model_tier == "gemini_3_8_flash"
    assert "/coder" in profile.slash_commands


def test_shipped_coder_profile_can_be_run_on_argon(shipped_config: AppConfig) -> None:
    """By a person choosing it, or a delegating model asking for it -- never Auto."""
    profile = shipped_profile(shipped_config, "coder")

    assert "gemini_4_argon" in (profile.allowed_model_tiers or [])
    assert "gemini_4_argon" in (profile.delegation_model_tiers or [])
    assert not profile.auto_model_tiers


def test_shipped_coder_profile_configures_no_sandbox_credentials(
    shipped_config: AppConfig,
) -> None:
    """Out of the box the sandbox holds no credential, so the profile keeps [C] only.

    `antigravity_config.environment` can inject a credential into the sandbox's
    egress proxy — a GitHub App token, say — which adds [B] to a profile that
    already reads the open web. That is a deployment's decision to make in its
    own config, so shipping it enabled here would hand every deployment the
    widening silently.
    """
    profile = shipped_profile(shipped_config, "coder")
    antigravity_config = profile.processing_config.antigravity_config

    assert antigravity_config is not None
    assert antigravity_config.environment is None


def test_shipped_coder_profile_grants_no_family_assistant_tools(
    shipped_config: AppConfig,
) -> None:
    """The agent works only from the request; it holds no [B] access.

    A deny-by-default `tools_policy` is not enough on its own: `global_tools_policy`
    is injected at the `profile` layer, which outranks the `defaults` layer this
    policy occupies, so the three globally granted tools have to be withheld
    explicitly or the profile is advertised as holding them.
    """
    profile = shipped_profile(shipped_config, "coder")
    assert profile.tools_policy is not None
    assert profile.tools_policy.default_decision == "deny"
    assert set(profile.excluded_global_tools) == {
        "read_text_attachment",
        "jq_query",
        "report_technical_problem",
    }


def test_shipped_coder_profile_is_a_sandbox_network_sink(
    shipped_config: AppConfig,
) -> None:
    """The profile declares the sink that gates reaching it at all.

    Without the declaration, a delegation to it is classified as an ordinary
    delegation and untrusted content could direct a code-execution agent --
    the thing the shipped matrix already denies for `spawn_worker`.
    """
    processing_config = shipped_profile(shipped_config, "coder").processing_config
    assert processing_config.taint_sink_class is SinkClass.SANDBOX_NETWORK


def test_shipped_coder_profile_does_not_override_the_rollout_mode(
    shipped_config: AppConfig,
) -> None:
    """The sink declaration rides the deployment's rollout; it does not preempt it.

    A profile-level `enforce` would apply the context-free tier x sink matrix to
    this one profile while the deployment is still measuring that matrix's
    false-positive rate -- and it is the friction that keeps the rollout in
    `observe` in the first place (see
    docs/design/runtime-taint-enforcement-operational-findings.md). Worse for
    this cell specifically: ambient high-tier prompt notes raise whole profiles
    to `unknown_external` irrespective of the request, so an override would
    refuse ordinary `/coder` turns, and the correction that document proposes
    for interactive `unknown_external -> sandbox_network` is confirmation
    rather than the hard denial an override produces.
    """
    profile = shipped_profile(shipped_config, "coder")

    merged = merge_taint_policy_config(
        base=shipped_config.taint_policy, profile=profile.taint_policy
    )

    assert merged.mode is shipped_config.taint_policy.mode


def test_the_shipped_matrix_adjudicates_a_sandbox_run_untrusted_content() -> None:
    """The reviewer may allow the run; an unavailable reviewer asks a human."""
    evaluator = TaintPolicyEvaluator(TaintPolicyConfig(mode=TaintPolicyMode.ENFORCE))
    state = TurnTaintState.empty().add_source(
        TaintSource(
            source_type=TaintSourceType.EMAIL,
            source_id="msg-1",
            tier=SourceTrustTier.UNKNOWN_EXTERNAL,
            labels=frozenset(),
            reason="Inbound email.",
        )
    )

    evaluation = evaluator.evaluate(state=state, sink_class=SinkClass.SANDBOX_NETWORK)

    assert evaluation.effective_outcome is TaintPolicyOutcome.ADJUDICATE
    assert evaluation.verdict_floor is None
    assert evaluation.fallback_outcome is TaintPolicyOutcome.CONFIRM


@pytest.mark.parametrize(
    ("tier", "reasoning_model"),
    [("gemini_3_8_flash", "gemini-3.8-flash"), ("gemini_4_argon", "gemini-4-argon")],
)
def test_each_coder_tier_reaches_the_agent_config_payload(
    shipped_config: AppConfig,
    provider_api_keys: None,
    tier: str,
    reasoning_model: str,
) -> None:
    """The client built for a tier sends the agent, reasoning with that tier's model."""
    profile = shipped_profile(shipped_config, "coder")
    assistant = Assistant(shipped_config, llm_client_overrides={})

    client = assistant._antigravity_tier_llm_client(profile, tier)  # pyright: ignore[reportPrivateUsage]

    assert isinstance(client, GoogleGenAIClient)
    kwargs = client._build_agent_create_kwargs([UserMessage(content="Do the thing.")])  # pyright: ignore[reportPrivateUsage]
    assert kwargs["agent"] == "antigravity-preview-09-2026"
    assert kwargs["agent_config"] == {"type": "antigravity", "model": reasoning_model}


def test_shipped_coder_profile_passes_its_own_startup_validation(
    shipped_config: AppConfig,
) -> None:
    """What ships must survive the guards that reject unrunnable combinations."""
    profile = shipped_profile(shipped_config, "coder")
    validate_profile_model_tier(profile, shipped_config.model_tiers)
    validate_antigravity_agent_config(
        profile.id, profile.processing_config, shipped_config.model
    )


def test_the_agent_in_a_retry_chain_is_rejected() -> None:
    """A fallback chat model would answer instead of running the task.

    The retry format carries no `antigravity_config`, so even as the primary
    the agent would run with none of its settings.
    """
    with pytest.raises(ValueError, match="retry_config, which is unsupported"):
        validate_antigravity_agent_config(
            "misconfigured",
            ProcessingConfig(
                retry_config=RetryConfig(
                    primary=RetryModelConfig(model="antigravity-preview-09-2026"),
                    fallback=RetryModelConfig(model="gemini-3.8-flash"),
                ),
            ),
            "gemini-3.8-flash",
        )


def _coder_system_instruction(shipped_config: AppConfig) -> str:
    """What the shipped `coder` profile actually sends as `system_instruction`."""
    profile = shipped_profile(shipped_config, "coder")
    processing_config = profile.processing_config
    assert processing_config.antigravity_config is not None

    client = GoogleGenAIClient(
        api_key="test",
        model=processing_config.antigravity_config.agent,
        antigravity_model="gemini-3.8-flash",
    )
    service = InteractionsAgentProcessingService(
        llm_client=client,
        tools_provider=LocalToolsProvider(definitions=[], implementations={}),
        service_config=ProcessingServiceConfig(
            prompts=processing_config.prompts,
            timezone=ZoneInfo("UTC"),
            history_budget_chars=100_000,
            history_max_age_hours=24,
            tools_config=ToolsConfig(),
            delegation_security_level=DelegationSecurityLevel.BLOCKED,
            id=profile.id,
            description=profile.description,
        ),
        context_providers=[],
        server_url="http://testserver",
        app_config=AppConfig(),
    )

    kwargs = client._build_agent_create_kwargs([  # pyright: ignore[reportPrivateUsage]
        SystemMessage(content=service.format_system_prompt(user_name="Tester")),
        UserMessage(content="Reverse this CSV."),
    ])
    return str(kwargs["system_instruction"])


def test_coder_system_instruction_carries_no_caller_facing_routing_guidance(
    shipped_config: AppConfig,
) -> None:
    """The profile's `description` is written to whoever picks a profile.

    It says things like "choose it over spawn_worker" in the second person; the
    agent reads that "you" as itself and is handed alternatives it cannot
    reach. What the agent is for is its own `system_prompt`'s job.
    """
    instruction = _coder_system_instruction(shipped_config)

    assert "spawn_worker" not in instruction
    assert "complex_tasks" not in instruction
    assert "Profile purpose:" not in instruction
    assert "You are a coding agent working for Tester" in instruction


def test_coder_system_instruction_is_unmarked(shipped_config: AppConfig) -> None:
    """Antigravity takes a first-class system field, so nothing labels it.

    The `System:` marker exists for the paths that have to carry system content
    as something else -- `generateContent` parts, Deep Research's single input
    string -- and prefixing a native `system_instruction` only spends the first
    line of the prompt saying what the field already says.
    """
    instruction = _coder_system_instruction(shipped_config)

    assert not instruction.startswith("System:")
    assert instruction.startswith("[Active Processing Profile: coder]")
