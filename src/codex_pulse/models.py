from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class UsageSample:
    observed_at: int
    primary_used_percent: float
    primary_window_minutes: int
    primary_resets_at: int
    secondary_used_percent: Optional[float]
    secondary_window_minutes: Optional[int]
    secondary_resets_at: Optional[int]
    credits_balance: Optional[float]
    credits_has_credits: bool
    credits_unlimited: bool
    ordinary_usage_allowed: Optional[bool]
    plan_type: Optional[str]
    limit_id: str = "codex"
    limit_reached_type: Optional[str] = None
    model_slug: Optional[str] = None
    primary_limit_available: bool = True

    @property
    def primary_remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.primary_used_percent)

    @property
    def secondary_remaining_percent(self) -> Optional[float]:
        if self.secondary_used_percent is None:
            return None
        return max(0.0, 100.0 - self.secondary_used_percent)

    @property
    def primary_exhausted(self) -> bool:
        return self.primary_limit_available and self.primary_used_percent >= 100.0

    @property
    def secondary_exhausted(self) -> bool:
        return (
            self.secondary_used_percent is not None
            and self.secondary_used_percent >= 100.0
        )

    def as_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["primary_remaining_percent"] = self.primary_remaining_percent
        value["secondary_remaining_percent"] = self.secondary_remaining_percent
        value["primary_exhausted"] = self.primary_exhausted
        value["secondary_exhausted"] = self.secondary_exhausted
        return value


@dataclass(frozen=True)
class Forecast:
    rate_15m: Optional[float]
    rate_60m: Optional[float]
    rate_24h: Optional[float]
    standard_rate: Optional[float]
    fast_rate: Optional[float]
    minutes_to_exhaustion: Optional[float]
    fast_minutes_to_exhaustion: Optional[float]
    estimated_exhaustion_at: Optional[int]
    reset_before_exhaustion: bool
    confidence: str
    sample_count: int
    span_minutes: float
    anomalous_consumption: bool
    credit_rate_60m: Optional[float] = None
    credit_minutes_remaining: Optional[float] = None
    limiting_window: Optional[str] = None
    fast_limiting_window: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Alert:
    event_key: str
    title: str
    body: str
    severity: str = "warning"
    related_event_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResumeTarget:
    thread_id: str
    thread_name: Optional[str]
    project_id: Optional[str]
    project_name: Optional[str]
    stopped_at: int

    @property
    def label(self) -> str:
        return self.thread_name or self.thread_id

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TurnUsage:
    turn_id: str
    thread_id: str
    root_turn_id: Optional[str]
    session_file: str
    started_at: Optional[int]
    completed_at: Optional[int]
    duration_ms: Optional[int]
    model: str
    effort: str
    cwd: Optional[str]
    project_name: str
    is_subagent: bool
    status: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0
    max_request_input_tokens: int = 0
    context_window: Optional[int] = None
    updated_at: int = 0

    @property
    def active_minutes(self) -> float:
        if self.started_at is None or self.completed_at is None:
            return 0.0
        return max(0.0, (self.completed_at - self.started_at) / 60.0)

    @property
    def cached_percent(self) -> Optional[float]:
        if self.input_tokens <= 0:
            return None
        return min(100.0, self.cached_input_tokens / self.input_tokens * 100.0)

    @property
    def attribution_weight(self) -> float:
        uncached = max(0, self.input_tokens - self.cached_input_tokens)
        # Cached input is not free, but it should not outweigh uncached context.
        return max(
            1.0,
            uncached
            + self.cached_input_tokens * 0.10
            + self.cache_write_input_tokens
            + self.output_tokens,
        )

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CycleResult:
    sample: UsageSample
    forecast: Forecast
    alerts: tuple[Alert, ...]
    resumed: bool = False
    resume_detail: Optional[str] = None
