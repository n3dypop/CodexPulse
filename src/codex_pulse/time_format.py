from __future__ import annotations

from typing import Optional


def approximate_duration(minutes: Optional[float]) -> str:
    """Format an approximate duration as ~DD:HH:MM."""
    if minutes is None:
        return "—"
    total_minutes = 0 if minutes <= 0 else max(1, int(round(minutes)))
    days, remaining_minutes = divmod(total_minutes, 1440)
    hours, rest = divmod(remaining_minutes, 60)
    return f"~{days:02d}:{hours:02d}:{rest:02d}"
