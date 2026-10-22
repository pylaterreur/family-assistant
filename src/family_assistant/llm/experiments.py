"""Assigning turns to the arms of a live model experiment.

The arm is a pure function of the experiment and the assignment unit, so it is
sticky without being stored: every turn of a conversation lands on the same
arm, and the turn record is the only record of the assignment. See
docs/design/live-model-experiments.md.
"""

import hashlib
from collections.abc import Mapping


def assign_arm(experiment_id: str, unit: str, arms: Mapping[str, int]) -> str:
    """The arm *unit* falls in, drawn with probability proportional to weight.

    Arms are taken in sorted order so the draw does not depend on how the
    config happened to list them.
    """
    if not arms:
        msg = f"Experiment '{experiment_id}' has no arms."
        raise ValueError(msg)
    digest = hashlib.sha256(f"{experiment_id}:{unit}".encode()).digest()
    total = sum(arms.values())
    point = int.from_bytes(digest[:8], "big") % total
    for arm in sorted(arms):
        point -= arms[arm]
        if point < 0:
            return arm
    raise AssertionError("unreachable: point is below the total weight")
