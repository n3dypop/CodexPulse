from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

from .config import MonitorConfig
from .models import Forecast, UsageSample


@dataclass(frozen=True)
class _WindowForecast:
    name: str
    rate_15m: Optional[float]
    rate_60m: Optional[float]
    rate_24h: Optional[float]
    standard_rate: Optional[float]
    fast_rate: Optional[float]
    minutes: Optional[float]
    fast_minutes: Optional[float]
    exhaustion_at: Optional[int]
    fast_exhaustion_at: Optional[int]
    reset_at: int
    confidence: str
    sample_count: int
    span_minutes: float
    anomalous: bool

    @property
    def reaches_before_reset(self) -> bool:
        return (
            self.exhaustion_at is not None
            and (self.reset_at <= 0 or self.exhaustion_at < self.reset_at)
        )

    @property
    def fast_reaches_before_reset(self) -> bool:
        return (
            self.fast_exhaustion_at is not None
            and (self.reset_at <= 0 or self.fast_exhaustion_at < self.reset_at)
        )


def _rates(
    samples: Sequence[UsageSample],
    used_percent: Callable[[UsageSample], Optional[float]],
) -> List[Tuple[int, float]]:
    values: List[Tuple[int, float]] = []
    for previous, current in zip(samples, samples[1:]):
        previous_used = used_percent(previous)
        current_used = used_percent(current)
        if previous_used is None or current_used is None:
            continue
        elapsed_minutes = (current.observed_at - previous.observed_at) / 60.0
        consumed = current_used - previous_used
        if elapsed_minutes <= 0 or consumed < 0:
            continue
        values.append((current.observed_at, consumed / elapsed_minutes))
    return values


def _ema(values: Iterable[float], alpha: float = 0.45) -> Optional[float]:
    result: Optional[float] = None
    for value in values:
        result = value if result is None else alpha * value + (1.0 - alpha) * result
    return result


def _rate_for_window(
    rates: Sequence[Tuple[int, float]], now: int, minutes: int
) -> Optional[float]:
    cutoff = now - minutes * 60
    return _ema(value for timestamp, value in rates if timestamp >= cutoff)


def _confidence(
    rates: Sequence[Tuple[int, float]],
    span_minutes: float,
    rate_15m: Optional[float],
    rate_60m: Optional[float],
) -> str:
    if len(rates) >= 8 and span_minutes >= 45:
        value = "高"
    elif len(rates) >= 3 and span_minutes >= 15:
        value = "中"
    else:
        value = "低"
    if rate_15m is not None and rate_60m is not None and rate_60m > 0:
        divergence = max(rate_15m, rate_60m) / max(
            min(rate_15m, rate_60m), 0.001
        )
        if divergence >= 2.5 and value == "高":
            value = "中"
    return value


def _window_forecast(
    *,
    name: str,
    samples: Sequence[UsageSample],
    current: UsageSample,
    reset_at: int,
    remaining: float,
    used_percent: Callable[[UsageSample], Optional[float]],
    same_window: Callable[[UsageSample], bool],
    config: MonitorConfig,
) -> _WindowForecast:
    window_samples = [
        sample
        for sample in samples
        if sample.observed_at <= current.observed_at and same_window(sample)
    ]
    rates = _rates(window_samples, used_percent)
    rate_15m = _rate_for_window(rates, current.observed_at, 15)
    rate_60m = _rate_for_window(rates, current.observed_at, 60)
    rate_24h = _rate_for_window(rates, current.observed_at, 24 * 60)

    def usable(value: Optional[float]) -> bool:
        return value is not None and value >= config.minimum_rate

    standard_rate = (
        rate_60m if usable(rate_60m) else rate_15m if usable(rate_15m) else rate_24h
    )
    if not usable(standard_rate):
        standard_rate = None
    fast_candidates = [value for value in (rate_15m, standard_rate) if usable(value)]
    fast_rate = max(fast_candidates) if fast_candidates else None
    minutes = remaining / standard_rate if standard_rate else None
    fast_minutes = remaining / fast_rate if fast_rate else None
    exhaustion_at = (
        current.observed_at + int(minutes * 60) if minutes is not None else None
    )
    fast_exhaustion_at = (
        current.observed_at + int(fast_minutes * 60)
        if fast_minutes is not None
        else None
    )
    span_minutes = (
        (window_samples[-1].observed_at - window_samples[0].observed_at) / 60.0
        if len(window_samples) > 1
        else 0.0
    )
    anomalous = bool(
        rate_15m is not None
        and rate_60m is not None
        and rate_15m >= config.anomaly_min_rate
        and rate_15m > rate_60m * config.anomaly_multiplier
        and len(rates) >= 3
    )
    return _WindowForecast(
        name=name,
        rate_15m=rate_15m,
        rate_60m=rate_60m,
        rate_24h=rate_24h,
        standard_rate=standard_rate,
        fast_rate=fast_rate,
        minutes=minutes,
        fast_minutes=fast_minutes,
        exhaustion_at=exhaustion_at,
        fast_exhaustion_at=fast_exhaustion_at,
        reset_at=reset_at,
        confidence=_confidence(rates, span_minutes, rate_15m, rate_60m),
        sample_count=len(window_samples),
        span_minutes=span_minutes,
        anomalous=anomalous,
    )


def _credit_rate(samples: Sequence[UsageSample], now: int) -> Optional[float]:
    cutoff = now - 3600
    values = []
    for previous, current in zip(samples, samples[1:]):
        if current.observed_at < cutoff:
            continue
        if previous.credits_balance is None or current.credits_balance is None:
            continue
        elapsed = (current.observed_at - previous.observed_at) / 60.0
        consumed = previous.credits_balance - current.credits_balance
        if elapsed > 0 and consumed >= 0:
            values.append(consumed / elapsed)
    return _ema(values)


def forecast_usage(
    samples: Sequence[UsageSample], config: MonitorConfig
) -> Forecast:
    if not samples:
        return Forecast(
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            False,
            "データ不足",
            0,
            0,
            False,
        )

    current = samples[-1]
    windows: list[_WindowForecast] = []
    if current.primary_limit_available:
        windows.append(
            _window_forecast(
                name="5-hour",
                samples=samples,
                current=current,
                reset_at=current.primary_resets_at,
                remaining=current.primary_remaining_percent,
                used_percent=lambda sample: (
                    sample.primary_used_percent
                    if sample.primary_limit_available
                    else None
                ),
                same_window=lambda sample: (
                    sample.primary_limit_available
                    and sample.primary_resets_at == current.primary_resets_at
                ),
                config=config,
            )
        )
    if (
        current.secondary_used_percent is not None
        and current.secondary_resets_at is not None
    ):
        windows.append(
            _window_forecast(
                name="weekly",
                samples=samples,
                current=current,
                reset_at=current.secondary_resets_at,
                remaining=current.secondary_remaining_percent or 0.0,
                used_percent=lambda sample: sample.secondary_used_percent,
                same_window=lambda sample: (
                    sample.secondary_used_percent is not None
                    and sample.secondary_resets_at == current.secondary_resets_at
                ),
                config=config,
            )
        )

    primary = next((value for value in windows if value.name == "5-hour"), None)
    weekly = next((value for value in windows if value.name == "weekly"), None)

    # Keep the 5-hour forecast as the default whenever that limit exists. The
    # weekly forecast replaces it only after both ETAs are calculable and the
    # weekly limit is demonstrably reached first. This prevents a long-lived
    # weekly history from taking over while a fresh 5-hour window is still
    # collecting its first samples.
    selected = primary or weekly
    if (
        primary is not None
        and weekly is not None
        and primary.minutes is not None
        and weekly.minutes is not None
        and weekly.reaches_before_reset
        and weekly.minutes < primary.minutes
    ):
        selected = weekly

    fast_selected: Optional[_WindowForecast] = None
    if primary is None:
        if weekly is not None and weekly.fast_reaches_before_reset:
            fast_selected = weekly
    elif primary.fast_minutes is not None:
        if primary.fast_reaches_before_reset:
            fast_selected = primary
        if (
            weekly is not None
            and weekly.fast_minutes is not None
            and weekly.fast_reaches_before_reset
            and weekly.fast_minutes < primary.fast_minutes
        ):
            fast_selected = weekly

    reset_before = bool(
        selected is not None
        and selected.minutes is not None
        and not selected.reaches_before_reset
    )

    credit_rate = _credit_rate(samples, current.observed_at)
    credit_minutes = None
    if (
        credit_rate is not None
        and credit_rate >= config.minimum_rate
        and current.credits_balance is not None
    ):
        credit_minutes = current.credits_balance / credit_rate

    return Forecast(
        rate_15m=selected.rate_15m if selected else None,
        rate_60m=selected.rate_60m if selected else None,
        rate_24h=selected.rate_24h if selected else None,
        standard_rate=selected.standard_rate if selected else None,
        fast_rate=fast_selected.fast_rate if fast_selected else None,
        minutes_to_exhaustion=selected.minutes if selected is not None else None,
        fast_minutes_to_exhaustion=(
            fast_selected.fast_minutes if fast_selected is not None else None
        ),
        estimated_exhaustion_at=selected.exhaustion_at if selected else None,
        reset_before_exhaustion=reset_before,
        confidence=selected.confidence if selected else "データ不足",
        sample_count=selected.sample_count if selected else 0,
        span_minutes=selected.span_minutes if selected else 0.0,
        anomalous_consumption=selected.anomalous if selected else False,
        credit_rate_60m=credit_rate,
        credit_minutes_remaining=credit_minutes,
        limiting_window=selected.name if selected else None,
        fast_limiting_window=fast_selected.name if fast_selected else None,
    )
