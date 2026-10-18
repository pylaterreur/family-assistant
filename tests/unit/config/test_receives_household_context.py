"""Which shipped profiles receive household context.

Content copied out of a Home conversation may enter only a profile that already
receives household context (docs/design/one-thread-chat-ux.md), so this pins
what the shipped profiles mean by it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from family_assistant.config_models import ProcessingConfig
from tests.unit.conftest import shipped_profile

if TYPE_CHECKING:
    from family_assistant.config_models import AppConfig

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize(
    ("profile_id", "expected"),
    [
        ("default_assistant", True),
        ("browser_profile", False),
        ("coder", False),
        ("media_analyst", False),
    ],
)
def test_shipped_profiles(
    shipped_config: AppConfig, profile_id: str, expected: bool
) -> None:
    profile = shipped_profile(shipped_config, profile_id)

    assert profile.processing_config.receives_household_context is expected


def test_any_excluded_provider_counts_as_receiving_none() -> None:
    config = ProcessingConfig.model_validate({
        "include_aggregated_context": True,
        "excluded_context_providers": ["known_users"],
    })

    assert config.receives_household_context is False
