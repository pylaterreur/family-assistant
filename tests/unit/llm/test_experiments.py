"""Live model experiments: arm assignment, the recorded envelope, and config."""

from collections import Counter
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from family_assistant.config_models import (
    AppConfig,
    ExperimentConfig,
    ModelTierConfig,
    RetryModelConfig,
    ServiceProfile,
)
from family_assistant.llm.experiments import assign_arm
from family_assistant.llm.model_selection import (
    ModelSelectionRequest,
    ModelTierEligibility,
    ModelTierOption,
    ResolvedModelSelection,
    model_selection_from_reasoning,
    resolve_model_selection,
    stamp_model_selection,
)

if TYPE_CHECKING:
    from family_assistant.llm.messages import MessageReasoningInfo

ARMS = {"claude_opus_5_5": 1, "gemini_4_argon": 1}


def test_assignment_is_stable_for_a_unit() -> None:
    first = assign_arm("exp", "conversation-1", ARMS)
    assert all(assign_arm("exp", "conversation-1", ARMS) == first for _ in range(5))


def test_assignment_does_not_depend_on_arm_order() -> None:
    reversed_arms = dict(reversed(list(ARMS.items())))
    for index in range(50):
        unit = f"conversation-{index}"
        assert assign_arm("exp", unit, ARMS) == assign_arm("exp", unit, reversed_arms)


def test_assignment_follows_the_weights() -> None:
    counts = Counter(
        assign_arm("exp", f"conversation-{index}", {"a": 3, "b": 1})
        for index in range(4000)
    )
    assert 0.70 < counts["a"] / 4000 < 0.80


def test_a_different_experiment_reshuffles() -> None:
    units = [f"conversation-{index}" for index in range(100)]
    first = [assign_arm("exp-1", unit, ARMS) for unit in units]
    second = [assign_arm("exp-2", unit, ARMS) for unit in units]
    assert first != second


_ELIGIBILITY = ModelTierEligibility(
    default_tier="standard",
    selectable=(ModelTierOption(id="standard", label="Standard"),),
    auto=frozenset({"standard"}),
    delegation=(ModelTierOption(id="gemini_4_argon", label="Argon"),),
)


def test_an_experiment_may_assign_a_delegation_preset() -> None:
    """Presets are what arms usually are: one specific model each."""
    selection = resolve_model_selection(
        _ELIGIBILITY,
        ModelSelectionRequest(tier="gemini_4_argon", source="experiment"),
        profile_id="default_assistant",
    )
    assert selection.tier == "gemini_4_argon"
    assert selection.source == "experiment"


def test_the_experiment_is_recorded_and_read_back() -> None:
    selection = ResolvedModelSelection(
        tier="gemini_4_argon",
        requested="gemini_4_argon",
        source="experiment",
        experiment_id="argon-vs-opus",
    )
    reasoning: MessageReasoningInfo = {}
    stamp_model_selection(reasoning, selection)

    assert reasoning.get("experiment_id") == "argon-vs-opus"
    assert reasoning.get("experiment_arm") == "gemini_4_argon"
    assert reasoning.get("model_tier_source") == "experiment"
    read_back = model_selection_from_reasoning(reasoning)
    assert read_back is not None
    assert read_back.experiment_id == "argon-vs-opus"
    assert read_back.frozen
    assert ResolvedModelSelection.from_json(selection.to_json()) == selection.freeze()


def _tier(model: str) -> ModelTierConfig:
    return ModelTierConfig(chain=[RetryModelConfig(provider="google", model=model)])


_TIERS = {"standard": _tier("flash"), "argon": _tier("argon"), "opus": _tier("opus")}


def test_an_arm_that_is_not_a_tier_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not model tiers: sonnet"):
        AppConfig(
            model_tiers=_TIERS,
            experiments=[
                ExperimentConfig(
                    id="exp",
                    profiles=["default_assistant"],
                    arms={"argon": 1, "sonnet": 1},
                )
            ],
        )


def test_a_profile_in_two_enabled_experiments_is_rejected() -> None:
    with pytest.raises(ValidationError, match="two enabled experiments"):
        AppConfig(
            model_tiers=_TIERS,
            service_profiles=[ServiceProfile(id="default_assistant")],
            experiments=[
                ExperimentConfig(
                    id="one",
                    profiles=["default_assistant"],
                    arms={"argon": 1, "opus": 1},
                ),
                ExperimentConfig(
                    id="two",
                    profiles=["default_assistant"],
                    arms={"argon": 1, "standard": 1},
                ),
            ],
        )


def test_only_an_enabled_experiment_enrolls_a_profile() -> None:
    config = AppConfig(
        model_tiers=_TIERS,
        service_profiles=[ServiceProfile(id="default_assistant")],
        experiments=[
            ExperimentConfig(
                id="old",
                profiles=["default_assistant"],
                arms={"argon": 1, "opus": 1},
                enabled=False,
            ),
            ExperimentConfig(
                id="new",
                profiles=["default_assistant"],
                arms={"argon": 1, "standard": 1},
            ),
        ],
    )
    experiment = config.experiment_for_profile("default_assistant")
    assert experiment is not None
    assert experiment.id == "new"
    assert config.experiment_for_profile("research") is None


def test_an_experiment_needs_two_arms_with_positive_weights() -> None:
    with pytest.raises(ValidationError):
        ExperimentConfig(id="exp", profiles=["p"], arms={"argon": 1})
    with pytest.raises(ValidationError, match="positive"):
        ExperimentConfig(id="exp", profiles=["p"], arms={"argon": 1, "opus": 0})
