from __future__ import annotations

import ctypes
import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BatteryStatus:
    percent: int
    state: str
    power_source: str
    estimate_minutes: Optional[int] = None


@dataclass(frozen=True)
class ComputerMetrics:
    observed_at: int
    cpu_percent: Optional[float]
    cpu_user_percent: Optional[float]
    cpu_system_percent: Optional[float]
    load_1m: float
    load_5m: float
    load_15m: float
    cpu_count: int
    architecture: str
    memory_used_bytes: Optional[int]
    memory_total_bytes: Optional[int]
    memory_used_percent: Optional[float]
    disk_used_bytes: int
    disk_total_bytes: int
    disk_used_percent: float
    active_uptime_seconds: int
    battery: Optional[BatteryStatus]


def parse_battery_status(output: str) -> Optional[BatteryStatus]:
    power_match = re.search(r"Now drawing from '([^']+)'", output)
    percent_match = re.search(r"(\d{1,3})%;", output)
    if percent_match is None:
        return None
    parts = output.split(";")
    state = parts[1].strip().title() if len(parts) > 1 else "Unknown"
    estimate_match = re.search(r"(\d+):(\d+) remaining", output)
    estimate = None
    if estimate_match is not None:
        estimate = int(estimate_match.group(1)) * 60 + int(estimate_match.group(2))
    return BatteryStatus(
        percent=max(0, min(100, int(percent_match.group(1)))),
        state=state,
        power_source=power_match.group(1) if power_match else "Unknown power",
        estimate_minutes=estimate,
    )


def collect_battery_status() -> Optional[BatteryStatus]:
    try:
        completed = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return parse_battery_status(completed.stdout)


def parse_memory_pressure(output: str) -> tuple[Optional[int], Optional[float]]:
    total_match = re.search(r"has (\d+) ", output)
    free_match = re.search(r"free percentage:\s*(\d+(?:\.\d+)?)%", output)
    total = int(total_match.group(1)) if total_match else None
    used_percent = 100.0 - float(free_match.group(1)) if free_match else None
    return total, used_percent


def _memory() -> tuple[Optional[int], Optional[int], Optional[float]]:
    try:
        completed = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None, None
    if completed.returncode != 0:
        return None, None, None
    total, used_percent = parse_memory_pressure(completed.stdout)
    used = (
        int(total * used_percent / 100)
        if total is not None and used_percent is not None
        else None
    )
    return used, total, used_percent


def cpu_percent_from_ticks(
    before: tuple[int, int, int, int], after: tuple[int, int, int, int]
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    deltas = [max(0, end - start) for start, end in zip(before, after)]
    total = sum(deltas)
    if total <= 0:
        return None, None, None
    user = (deltas[0] + deltas[3]) / total * 100
    system = deltas[1] / total * 100
    busy = 100 - deltas[2] / total * 100
    return busy, user, system


def _cpu_ticks() -> tuple[int, int, int, int]:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    library.mach_host_self.restype = ctypes.c_uint
    ticks = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(4)
    result = library.host_statistics(
        library.mach_host_self(),
        3,
        ctypes.byref(ticks),
        ctypes.byref(count),
    )
    if result != 0 or count.value < 4:
        raise OSError(f"host_statistics failed: {result}")
    return tuple(int(value) for value in ticks)  # type: ignore[return-value]


def _cpu(interval_seconds: float = 0.35) -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        before = _cpu_ticks()
        time.sleep(max(0.1, interval_seconds))
        return cpu_percent_from_ticks(before, _cpu_ticks())
    except (OSError, AttributeError):
        return None, None, None


def collect_computer_metrics() -> ComputerMetrics:
    cpu_percent, cpu_user, cpu_system = _cpu()
    memory_used, memory_total, memory_percent = _memory()
    load_1m, load_5m, load_15m = os.getloadavg()
    disk = shutil.disk_usage(Path.home())
    disk_percent = disk.used / disk.total * 100 if disk.total else 0.0
    return ComputerMetrics(
        observed_at=int(time.time()),
        cpu_percent=cpu_percent,
        cpu_user_percent=cpu_user,
        cpu_system_percent=cpu_system,
        load_1m=load_1m,
        load_5m=load_5m,
        load_15m=load_15m,
        cpu_count=max(1, os.cpu_count() or 1),
        architecture=platform.machine() or "Unknown",
        memory_used_bytes=memory_used,
        memory_total_bytes=memory_total,
        memory_used_percent=memory_percent,
        disk_used_bytes=disk.used,
        disk_total_bytes=disk.total,
        disk_used_percent=disk_percent,
        active_uptime_seconds=max(0, int(time.monotonic())),
        battery=collect_battery_status(),
    )
