from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable, Optional

from .models import TurnUsage
from .storage import Storage


CONFIDENCE_RANK = {"unavailable": 0, "low": 1, "medium": 2, "high": 3}


def _confidence(values: Iterable[str]) -> str:
    available = [
        value
        for value in values
        if value in CONFIDENCE_RANK and value != "unavailable"
    ]
    if not available:
        return "unavailable"
    return min(available, key=lambda value: CONFIDENCE_RANK[value])


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def rebuild_attributions(storage: Storage, since: int) -> int:
    """Attribute aggregate five-hour usage deltas to active turns.

    The account API exposes one aggregate percentage. A single active turn is high
    confidence; concurrent turns are allocated by a transparent token-weight proxy.
    """

    samples = storage.recent_samples(since)
    written = 0
    for previous, current in zip(samples, samples[1:]):
        if current.primary_resets_at != previous.primary_resets_at:
            storage.replace_attributions(current.observed_at, [])
            continue
        delta = current.primary_used_percent - previous.primary_used_percent
        if delta <= 0:
            storage.replace_attributions(current.observed_at, [])
            continue
        turns = storage.turns_overlapping(previous.observed_at, current.observed_at)
        if not turns:
            storage.replace_attributions(current.observed_at, [])
            continue
        interval = current.observed_at - previous.observed_at
        if len(turns) == 1 and interval <= 900:
            confidence = "high"
        elif len(turns) <= 3 and interval <= 1800:
            confidence = "medium"
        else:
            confidence = "low"
        total_weight = sum(turn.attribution_weight for turn in turns)
        values = [
            (
                turn.turn_id,
                delta * turn.attribution_weight / total_weight,
                confidence,
            )
            for turn in turns
        ]
        storage.replace_attributions(current.observed_at, values)
        written += len(values)
    return written


def _profile(rows: list[dict[str, Any]], key_names: tuple[str, ...]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if int(row.get("total_tokens") or 0) <= 0:
            continue
        groups[tuple(str(row.get(name) or "Unknown") for name in key_names)].append(row)

    results: list[dict[str, Any]] = []
    for key, values in groups.items():
        completed = [row for row in values if row.get("completed_at") is not None]
        sample_count = len(completed)
        if sample_count == 0:
            continue
        duration_minutes = sum(
            max(0.0, float(row.get("duration_ms") or 0) / 60000.0)
            for row in completed
        )
        attributed = sum(float(row.get("attributed_percent") or 0.0) for row in values)
        input_tokens = sum(int(row.get("input_tokens") or 0) for row in completed)
        cached_tokens = sum(
            int(row.get("cached_input_tokens") or 0) for row in completed
        )
        reasoning_values = [
            float(row.get("reasoning_output_tokens") or 0) for row in completed
        ]
        output_values = [float(row.get("output_tokens") or 0) for row in completed]
        results.append(
            {
                **dict(zip(key_names, key)),
                "sample_count": sample_count,
                "active_minutes": duration_minutes,
                "attributed_percent": attributed,
                "percent_per_active_min": (
                    attributed / duration_minutes if duration_minutes > 0 else None
                ),
                "percent_per_turn": attributed / sample_count,
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_tokens,
                "cache_percent": (
                    cached_tokens / input_tokens * 100 if input_tokens > 0 else None
                ),
                "output_per_turn": statistics.median(output_values),
                "reasoning_per_turn": statistics.median(reasoning_values),
                "reasoning_p90": _percentile(reasoning_values, 0.90),
                "success_percent": sum(
                    1 for row in completed if row.get("status") == "completed"
                )
                / sample_count
                * 100,
                "confidence": _confidence(
                    str(row.get("attribution_confidence") or "unavailable")
                    for row in values
                ),
            }
        )
    results.sort(
        key=lambda item: (
            item["attributed_percent"],
            item["sample_count"],
        ),
        reverse=True,
    )
    return results


def model_profiles(storage: Storage, since: int) -> list[dict[str, Any]]:
    return _profile(storage.analytics_rows(since), ("model", "effort"))


def project_profiles(storage: Storage, since: int) -> list[dict[str, Any]]:
    return _profile(storage.analytics_rows(since), ("project_name",))


def recent_consumers(storage: Storage, since: int, limit: int = 8) -> list[dict[str, Any]]:
    rows = [row for row in storage.analytics_rows(since) if row.get("completed_at")]
    rows.sort(
        key=lambda row: (
            float(row.get("attributed_percent") or 0.0),
            int(row.get("total_tokens") or 0),
        ),
        reverse=True,
    )
    return rows[:limit]


def efficiency_findings(storage: Storage, since: int, limit: int = 6) -> list[dict[str, Any]]:
    rows = [
        row
        for row in storage.analytics_rows(since)
        if row.get("completed_at") and int(row.get("input_tokens") or 0) > 0
    ]
    baselines: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        baselines[
            (
                str(row.get("project_name") or "Unknown"),
                str(row.get("model") or "unknown"),
                str(row.get("effort") or "unknown"),
            )
        ].append(row)
    findings: list[dict[str, Any]] = []
    for values in baselines.values():
        if len(values) < 3:
            continue
        reasoning_median = statistics.median(
            int(row.get("reasoning_output_tokens") or 0) for row in values
        )
        request_median = statistics.median(
            int(row.get("max_request_input_tokens") or 0) for row in values
        )
        for row in values:
            input_tokens = int(row.get("input_tokens") or 0)
            cached = int(row.get("cached_input_tokens") or 0)
            cache_percent = cached / input_tokens * 100 if input_tokens else 0.0
            reasoning = int(row.get("reasoning_output_tokens") or 0)
            request_input = int(row.get("max_request_input_tokens") or 0)
            score = 0.0
            reasons: list[str] = []
            if reasoning_median > 0 and reasoning >= reasoning_median * 2.5:
                score += reasoning / reasoning_median
                reasons.append(f"reasoning {reasoning / reasoning_median:.1f}× median")
            if request_median > 0 and request_input >= request_median * 1.8:
                score += request_input / request_median
                reasons.append(f"context {request_input / request_median:.1f}× median")
            if cache_percent < 50 and input_tokens >= 50_000:
                score += 2.0
                reasons.append(f"cache {cache_percent:.0f}%")
            if reasons:
                findings.append({**row, "score": score, "reasons": reasons})
    findings.sort(key=lambda row: float(row["score"]), reverse=True)
    return findings[:limit]


def forecast_accuracy(storage: Storage, since: int) -> dict[str, Any]:
    rows = storage.prediction_rows(since)
    exhausted = [
        row
        for row in rows
        if row.get("outcome") == "exhausted"
        and row.get("actual_exhaustion_at") is not None
    ]
    errors = [
        (int(row["estimated_exhaustion_at"]) - int(row["actual_exhaustion_at"]))
        / 60.0
        for row in exhausted
    ]
    return {
        "prediction_count": len(rows),
        "exhaustion_count": len(exhausted),
        "reset_first_count": sum(1 for row in rows if row.get("outcome") == "reset_first"),
        "median_absolute_error_minutes": (
            statistics.median(abs(value) for value in errors) if errors else None
        ),
        "within_15_minutes_percent": (
            sum(abs(value) <= 15 for value in errors) / len(errors) * 100
            if errors
            else None
        ),
        "median_bias_minutes": statistics.median(errors) if errors else None,
    }


def advisor(
    storage: Storage,
    since: int,
    remaining_percent: float,
    minimum_samples: int,
) -> dict[str, Any]:
    profiles = model_profiles(storage, since)
    eligible = [
        profile
        for profile in profiles
        if profile["model"] != "codex-auto-review"
        and profile["sample_count"] >= minimum_samples
        and profile["percent_per_active_min"] is not None
        and profile["percent_per_active_min"] > 0
    ]
    latest = storage.recent_turns(since, limit=1)
    current_key = (
        (latest[0].model, latest[0].effort) if latest else (None, None)
    )
    current = next(
        (
            profile
            for profile in eligible
            if (profile["model"], profile["effort"]) == current_key
        ),
        None,
    )
    alternatives = sorted(
        eligible,
        key=lambda profile: float(profile["percent_per_active_min"]),
    )
    for profile in alternatives:
        profile["estimated_minutes"] = remaining_percent / float(
            profile["percent_per_active_min"]
        )
    return {
        "current": current,
        "alternatives": alternatives,
        "insufficient_data": len(eligible) == 0,
    }


def simulate(
    storage: Storage,
    since: int,
    model: str,
    effort: str,
    remaining_percent: float,
) -> Optional[dict[str, Any]]:
    profile = next(
        (
            value
            for value in model_profiles(storage, since)
            if value["model"].casefold() == model.casefold()
            and value["effort"].casefold() == effort.casefold()
        ),
        None,
    )
    if profile is None or not profile.get("percent_per_active_min"):
        return None
    return {
        **profile,
        "estimated_minutes": remaining_percent
        / float(profile["percent_per_active_min"]),
    }
