#!/usr/bin/env python3
"""GNU Parallel --limit helper that holds back new jobs under memory pressure.

Usage is measured from cgroup v2 where one sets a limit (CI and containers), or
on macOS from ``vm_stat``. Each source has its own default threshold because
the two ratios do not mean the same thing; an explicit threshold argument
overrides both.

The macOS ratio is Activity Monitor's "Memory Used" over physical memory: app
memory, wired memory and the compressor's footprint. File cache is left out,
because the system drops it on demand, while compressed pages are what fills up
when a run starts to thrash. In a 9-CPU, 16 GB VM a healthy full test run at 16
jobs peaked at 0.85 and one that thrashed had filled the machine, hence 0.90.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

CGROUP_DEFAULT_THRESHOLD = 0.80
MACOS_DEFAULT_THRESHOLD = 0.90

_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_FIELDS = {
    "anonymous": "Anonymous pages",
    "purgeable": "Pages purgeable",
    "wired": "Pages wired down",
    "compressor": "Pages occupied by compressor",
}


class MemoryReading(NamedTuple):
    """A usage ratio and the threshold that suits where it came from."""

    ratio: float
    default_threshold: float


def cgroup_usage_ratio(cgroup_path: Path = Path("/sys/fs/cgroup")) -> float | None:
    """Return current/max cgroup memory usage, or None when no max is visible."""
    try:
        current = int((cgroup_path / "memory.current").read_text())
        maximum_text = (cgroup_path / "memory.max").read_text().strip()
    except FileNotFoundError:
        return None

    if maximum_text == "max":
        return None

    maximum = int(maximum_text)
    if maximum <= 0:
        return None

    return current / maximum


def vm_stat_usage_ratio(vm_stat_output: str, total_bytes: int) -> float:
    """Return macOS "Memory Used" over physical memory from ``vm_stat`` output."""
    page_size_match = _VM_STAT_PAGE_SIZE.search(vm_stat_output)
    if page_size_match is None:
        raise ValueError("vm_stat output does not state its page size")

    pages: dict[str, int] = {}
    for key, label in _VM_STAT_FIELDS.items():
        match = re.search(rf"^{re.escape(label)}:\s+(\d+)\.", vm_stat_output, re.M)
        if match is None:
            raise ValueError(f"vm_stat output has no '{label}' line")
        pages[key] = int(match.group(1))

    used_pages = (
        pages["anonymous"] - pages["purgeable"] + pages["wired"] + pages["compressor"]
    )
    return used_pages * int(page_size_match.group(1)) / total_bytes


def macos_usage_ratio() -> float:
    """Measure the running Mac's memory usage ratio."""
    vm_stat_output = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, check=True
    ).stdout
    total_bytes = int(
        subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, check=True
        ).stdout
    )
    return vm_stat_usage_ratio(vm_stat_output, total_bytes)


def memory_reading() -> MemoryReading | None:
    """Return this machine's memory usage, or None when nothing measures it."""
    ratio = cgroup_usage_ratio()
    if ratio is not None:
        return MemoryReading(ratio, CGROUP_DEFAULT_THRESHOLD)
    if sys.platform == "darwin":
        return MemoryReading(macos_usage_ratio(), MACOS_DEFAULT_THRESHOLD)
    return None


def main() -> int:
    """Exit using GNU Parallel --limit semantics: 0 starts a job, 1 waits."""
    try:
        reading = memory_reading()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        # GNU Parallel retries a waiting job forever, so failing closed would
        # hang the run without a word; say why and let the job start.
        print(
            f"memory_gate: cannot measure memory, not gating: {error}", file=sys.stderr
        )
        return 0
    if reading is None:
        return 0

    threshold = float(sys.argv[1]) if len(sys.argv) > 1 else reading.default_threshold
    return 1 if reading.ratio >= threshold else 0


if __name__ == "__main__":
    raise SystemExit(main())
