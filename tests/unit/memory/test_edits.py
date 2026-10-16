"""The shape a proposed memory edit list must take."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from family_assistant.memory.edits import MemoryEditList

pytestmark = pytest.mark.no_db


def test_a_misnamed_key_is_named_in_the_first_refusal() -> None:
    """A model that writes 'text' for 'entry' learns that from one refusal.

    The curator has one retry. If the unknown key were silently dropped, the
    first refusal would mention only the missing 'op' and 'note_title', and the
    second would be the first to say 'entry' is missing.
    """
    with pytest.raises(ValidationError) as excinfo:
        MemoryEditList.model_validate({
            "edits": [
                {
                    "type": "add",
                    "note": "Garden",
                    "text": "Andrew keeps the hedge on the rock wall.",
                    "message_ids": [1],
                }
            ]
        })

    locations = {tuple(error["loc"]) for error in excinfo.value.errors()}
    assert ("edits", 0, "op") in locations
    assert ("edits", 0, "note_title") in locations
    assert ("edits", 0, "text") in locations
    assert ("edits", 0, "type") in locations
    assert ("edits", 0, "note") in locations


def test_an_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MemoryEditList.model_validate({
            "edits": [{"op": "add", "note_title": "Garden", "entry": "A fact."}],
            "reason": "because",
        })

    assert ("reason",) in {tuple(error["loc"]) for error in excinfo.value.errors()}


def test_a_well_formed_list_is_accepted() -> None:
    proposal = MemoryEditList.model_validate({
        "edits": [
            {
                "op": "add",
                "note_title": "Garden",
                "entry": "A fact.",
                "message_ids": [1],
            }
        ]
    })

    assert proposal.edits[0].entry == "A fact."
