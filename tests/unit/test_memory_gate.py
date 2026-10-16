"""Tests for the GNU Parallel memory gate used by the adaptive pytest runner."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

# Captured from a 16 GB macOS VM (1,048,576 pages of 16 KiB).
VM_STAT_OUTPUT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                              295688.
Pages active:                            210566.
Pages inactive:                          296153.
Pages speculative:                        35065.
Pages throttled:                              0.
Pages wired down:                        139765.
Pages purgeable:                            799.
"Translation faults":               39585012675.
Pages copy-on-write:                  602774435.
Pages zero filled:                   1250134838.
Pages reactivated:                  10560336165.
Pages purged:                          86988230.
File-backed pages:                       189931.
Anonymous pages:                         351853.
Pages stored in compressor:              839121.
Pages occupied by compressor:             70349.
Decompressions:                     33189440773.
"""
TOTAL_BYTES = 17179869184


def _load_gate() -> ModuleType:
    script_path = Path(__file__).parents[2] / "scripts" / "memory_gate.py"
    spec = importlib.util.spec_from_file_location("memory_gate", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vm_stat_ratio_counts_app_wired_and_compressed_memory() -> None:
    gate = _load_gate()

    ratio = gate.vm_stat_usage_ratio(VM_STAT_OUTPUT, TOTAL_BYTES)

    used_pages = 351853 - 799 + 139765 + 70349
    assert ratio == pytest.approx(used_pages / 1048576)


def test_vm_stat_ratio_rejects_output_missing_a_field() -> None:
    gate = _load_gate()
    output = VM_STAT_OUTPUT.replace("Pages occupied by compressor", "Pages renamed")

    with pytest.raises(ValueError, match="Pages occupied by compressor"):
        gate.vm_stat_usage_ratio(output, TOTAL_BYTES)


def test_cgroup_ratio_reads_current_over_max(tmp_path: Path) -> None:
    gate = _load_gate()
    (tmp_path / "memory.current").write_text("750\n")
    (tmp_path / "memory.max").write_text("1000\n")

    assert gate.cgroup_usage_ratio(tmp_path) == pytest.approx(0.75)


def test_cgroup_without_a_limit_is_not_a_reading(tmp_path: Path) -> None:
    gate = _load_gate()
    (tmp_path / "memory.current").write_text("750\n")
    (tmp_path / "memory.max").write_text("max\n")

    assert gate.cgroup_usage_ratio(tmp_path) is None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["memory_gate.py"], 0),
        (["memory_gate.py", "0.80"], 1),
    ],
    ids=["platform-default-admits", "explicit-threshold-holds-back"],
)
def test_threshold_defaults_to_the_reading_source(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected: int
) -> None:
    gate = _load_gate()
    monkeypatch.setattr(gate, "memory_reading", lambda: gate.MemoryReading(0.85, 0.90))
    monkeypatch.setattr(sys, "argv", argv)

    assert gate.main() == expected


def test_unmeasurable_memory_admits_the_job_and_says_why(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """GNU Parallel retries a held-back job forever, so failing closed would hang."""
    gate = _load_gate()

    def fail() -> None:
        raise subprocess.CalledProcessError(1, ["vm_stat"])

    monkeypatch.setattr(gate, "memory_reading", fail)
    monkeypatch.setattr(sys, "argv", ["memory_gate.py"])

    assert gate.main() == 0
    assert "cannot measure memory" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "darwin", reason="reads this Mac's vm_stat")
def test_macos_reading_is_a_plausible_fraction_of_memory() -> None:
    gate = _load_gate()

    assert 0.0 < gate.macos_usage_ratio() < 1.0
