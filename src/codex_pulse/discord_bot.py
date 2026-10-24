from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import tasks

from .analytics import (
    advisor,
    efficiency_findings,
    forecast_accuracy,
    model_profiles,
    project_profiles,
    recent_consumers,
    simulate,
)
from .config import AppConfig, MonitorConfig
from .computer_use import ComputerUseStatus, detect_computer_use
from .models import Alert, CycleResult, Forecast, UsageSample
from .service import MonitorService
from .screen_capture import (
    ScreenCaptureError,
    capture_screenshots,
    fit_screenshot_for_upload,
)
from .storage import Storage
from .system_metrics import (
    BatteryStatus,
    ComputerMetrics,
    collect_battery_status,
    collect_computer_metrics,
)
from .time_format import approximate_duration


LOGGER = logging.getLogger(__name__)
TEST_NOTIFICATION_DELAY_SECONDS = 10
WAKE_CHECK_SECONDS = 15
WAKE_GAP_THRESHOLD_SECONDS = 60
NOTIFICATION_CLEANUP_SECONDS = 15
NOTIFICATION_RECOVERY_HISTORY_LIMIT = 200
SECTION_SPACER = "\n\u200b"
COMMAND_COLOR = discord.Color.blurple()
_AUTO_DELETE_RE = re.compile(
    r"Auto-delete(?:s in)?\s*(?:(?:·|:)\s*)?(\d+)\s*(?:seconds?|sec)",
    re.IGNORECASE,
)


def _local_time(timestamp: Optional[int], timezone: ZoneInfo) -> str:
    if timestamp is None:
        return "—"
    return datetime.fromtimestamp(timestamp, timezone).strftime("%b %d, %H:%M")


def _duration(minutes: Optional[float]) -> str:
    return approximate_duration(minutes)


def _forecast_duration(minutes: Optional[float]) -> str:
    return _duration(minutes).removeprefix("~")


def _history_duration(minutes: float) -> str:
    total_minutes = max(0, int(round(minutes)))
    days, remaining_minutes = divmod(total_minutes, 1440)
    hours, rest = divmod(remaining_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{rest}m")
    return " ".join(parts)


def _rate(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.2f}%/min"


def _size(value: int) -> str:
    amount = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "—"


def _battery_text(battery: Optional[BatteryStatus]) -> str:
    if battery is None:
        return "Not available"
    state = (
        battery.state
        if battery.state.casefold() not in {"", "unknown"}
        else battery.power_source
    )
    value = (
        f"`{_progress_bar(battery.percent)}`  **{battery.percent}%**\n{state}"
    )
    if battery.estimate_minutes is not None:
        value += f" · `{_forecast_duration(battery.estimate_minutes)}`"
    return value


def _cpu_text(metrics: Optional[ComputerMetrics]) -> str:
    if metrics is None or metrics.cpu_percent is None:
        return "Not available"
    user = metrics.cpu_user_percent or 0.0
    system = metrics.cpu_system_percent or 0.0
    return (
        f"`{_progress_bar(metrics.cpu_percent)}`  "
        f"**{metrics.cpu_percent:.1f}%**\n"
        f"User `{user:.1f}%` · System `{system:.1f}%`"
    )


def _memory_text(metrics: Optional[ComputerMetrics]) -> str:
    if metrics is None or metrics.memory_used_percent is None:
        return "Not available"
    detail = ""
    if (
        metrics.memory_used_bytes is not None
        and metrics.memory_total_bytes is not None
    ):
        detail = (
            f"\n`{_size(metrics.memory_used_bytes)}` / "
            f"`{_size(metrics.memory_total_bytes)}`"
        )
    return (
        f"`{_progress_bar(metrics.memory_used_percent)}`  "
        f"**{metrics.memory_used_percent:.0f}%**{detail}"
    )


def _computer_use_footer_text(
    status: Optional[ComputerUseStatus], _reference: int
) -> str:
    if status is None or status.state == "unavailable":
        return "⚪ Computer Use"
    if status.state == "active":
        return "🟢 Computer Use"
    if status.state == "used_this_turn":
        return "🟡 Computer Use"
    return "⚪ Computer Use"


def _progress_bar(percent: float, width: int = 16) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = round(clamped / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _countdown(timestamp: Optional[int], reference: int) -> str:
    if timestamp is None:
        return "—"
    total_minutes = max(0, round((int(timestamp) - int(reference)) / 60))
    days, remaining_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(remaining_minutes, 60)
    return f"{days:02d}:{hours:02d}:{minutes:02d}"


def _confidence(value: str) -> str:
    return {"低": "Low", "中": "Medium", "高": "High"}.get(value, value)


def _footer(*parts: str) -> str:
    return " • ".join(part for part in parts if part)


def _system_sleep_detected(
    previous_wall: float,
    previous_monotonic: float,
    current_wall: float,
    current_monotonic: float,
    threshold_seconds: float = WAKE_GAP_THRESHOLD_SECONDS,
) -> bool:
    wall_elapsed = max(0.0, current_wall - previous_wall)
    monotonic_elapsed = max(0.0, current_monotonic - previous_monotonic)
    return wall_elapsed - monotonic_elapsed >= threshold_seconds


def _notification_ttl_seconds(message: Any) -> Optional[int]:
    for embed in getattr(message, "embeds", ()):
        footer = getattr(embed, "footer", None)
        footer_text = getattr(footer, "text", None)
        if not footer_text:
            continue
        match = _AUTO_DELETE_RE.search(footer_text)
        if match is not None:
            return max(1, int(match.group(1)))
    return None


def _status_color(sample: UsageSample, forecast: Forecast) -> discord.Color:
    if (
        sample.primary_exhausted
        or sample.secondary_exhausted
        or sample.ordinary_usage_allowed is False
    ):
        return discord.Color.red()
    if (
        forecast.fast_minutes_to_exhaustion is not None
        and not forecast.reset_before_exhaustion
    ):
        return discord.Color.orange()
    return discord.Color.green()


def _forecast_limit_label(sample: UsageSample, forecast: Forecast) -> str:
    if forecast.limiting_window == "weekly":
        return "weekly"
    if forecast.limiting_window == "5-hour":
        return "5-hour"
    if (
        not sample.primary_limit_available
        and sample.secondary_used_percent is not None
    ):
        return "weekly"
    return "5-hour"


def _forecast_headline(sample: UsageSample, forecast: Forecast) -> str:
    if sample.secondary_exhausted:
        return "## LIMIT REACHED\nWeekly usage is unavailable until reset."
    if sample.primary_exhausted:
        return "## LIMIT REACHED\n5-hour usage is unavailable until reset."
    if sample.ordinary_usage_allowed is False:
        return "## LIMIT REACHED\nCodex usage is unavailable until reset."
    if not sample.primary_limit_available and sample.secondary_used_percent is None:
        return "## NO ACTIVE LIMIT\nNo usage limit is reported for this plan."
    if forecast.reset_before_exhaustion:
        return "## NO LIMIT EXPECTED\nA reset occurs before the projected limit."
    if forecast.minutes_to_exhaustion is None:
        return "## CALCULATING\nMore usage data is required."
    label = _forecast_limit_label(sample, forecast)
    return (
        f"## {_forecast_duration(forecast.minutes_to_exhaustion)}\n"
        f"Estimated time to reach the {label} limit."
    )


def _primary_limit_text(sample: UsageSample) -> str:
    if not sample.primary_limit_available:
        return (
            f"`{_progress_bar(100.0)}`  **No limit**\n"
            "Not reported for this plan"
        )
    return (
        f"`{_progress_bar(sample.primary_remaining_percent)}`  "
        f"**{sample.primary_remaining_percent:.0f}%**\n"
        f"Reset `{_countdown(sample.primary_resets_at, sample.observed_at)}`"
    )


def _account_text(
    sample: UsageSample, forecast: Optional[Forecast] = None
) -> str:
    if sample.credits_unlimited:
        return "Unlimited"
    if sample.credits_balance is None:
        return "Not reported"
    balance = sample.credits_balance
    unit = "credit" if balance == 1 else "credits"
    value = f"**{balance:g} {unit} remaining**"
    if forecast is not None and forecast.credit_minutes_remaining is not None:
        value += (
            f" · {_forecast_duration(forecast.credit_minutes_remaining)} estimated"
        )
    return value


def _set_command_footer(embed: discord.Embed, *parts: str) -> None:
    embed.set_footer(text=_footer("Private response", *parts))


def _command_card(
    title: str,
    *,
    description: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"CodexPulse · {title}",
        description=description,
        color=COMMAND_COLOR,
        timestamp=timestamp,
    )
    _set_command_footer(embed)
    return embed


def alert_embed(
    alert: Alert,
    timezone: ZoneInfo,
    delete_seconds: int,
    timestamp: Optional[datetime] = None,
    footer_icon_url: Optional[str] = None,
) -> discord.Embed:
    colors = {
        "critical": discord.Color.red(),
        "warning": discord.Color.orange(),
        "success": discord.Color.green(),
    }
    embed = discord.Embed(
        title=f"CodexPulse · {alert.title}",
        description=alert.body,
        color=colors.get(alert.severity, discord.Color.orange()),
        timestamp=timestamp or datetime.now(timezone),
    )
    footer_text = f"Auto-delete: {delete_seconds} sec"
    if footer_icon_url:
        embed.set_footer(text=footer_text, icon_url=footer_icon_url)
    else:
        embed.set_footer(text=footer_text)
    return embed


def _pressure_alert_triggered(
    storage: Storage,
    name: str,
    value: Optional[float],
    alert_threshold: float,
    recovery_threshold: float,
    consecutive_samples: int,
) -> bool:
    active_key = f"system_alert:{name}:active"
    streak_key = f"system_alert:{name}:streak"
    active = bool(storage.get_state(active_key, False))
    streak = int(storage.get_state(streak_key, 0) or 0)
    if value is None:
        storage.set_state(streak_key, 0)
        return False
    if value >= alert_threshold:
        streak += 1
        storage.set_state(streak_key, streak)
        if streak >= consecutive_samples and not active:
            storage.set_state(active_key, True)
            return True
        return False
    storage.set_state(streak_key, 0)
    if value <= min(recovery_threshold, alert_threshold - 1):
        storage.set_state(active_key, False)
    return False


def system_metric_alerts(
    metrics: ComputerMetrics,
    storage: Storage,
    config: MonitorConfig,
) -> tuple[Alert, ...]:
    alerts: list[Alert] = []
    if _pressure_alert_triggered(
        storage,
        "cpu",
        metrics.cpu_percent,
        config.cpu_alert_percent,
        config.cpu_recovery_percent,
        config.resource_alert_consecutive_samples,
    ):
        alerts.append(
            Alert(
                f"system:cpu:{metrics.observed_at}",
                "CPU pressure is high",
                (
                    f"`{_progress_bar(metrics.cpu_percent)}`  "
                    f"**{metrics.cpu_percent:.1f}%**\n"
                    f"Load `{metrics.load_1m:.2f}` / `{metrics.load_5m:.2f}` / "
                    f"`{metrics.load_15m:.2f}`"
                ),
                "warning",
            )
        )
    if _pressure_alert_triggered(
        storage,
        "memory",
        metrics.memory_used_percent,
        config.memory_alert_percent,
        config.memory_recovery_percent,
        config.resource_alert_consecutive_samples,
    ):
        memory_detail = ""
        if (
            metrics.memory_used_bytes is not None
            and metrics.memory_total_bytes is not None
        ):
            memory_detail = (
                f" · `{_size(metrics.memory_used_bytes)}` / "
                f"`{_size(metrics.memory_total_bytes)}`"
            )
        alerts.append(
            Alert(
                f"system:memory:{metrics.observed_at}",
                "Memory pressure is high",
                (
                    f"`{_progress_bar(metrics.memory_used_percent)}`  "
                    f"**{metrics.memory_used_percent:.0f}%**\n"
                    f"{memory_detail.removeprefix(' · ') or 'Memory pressure is elevated.'}"
                ),
                "warning",
            )
        )

    battery = metrics.battery
    battery_active_key = "system_alert:battery:active"
    battery_active = bool(storage.get_state(battery_active_key, False))
    if battery is not None:
        state = battery.state.casefold()
        source = battery.power_source.casefold()
        discharging = state == "discharging" or "battery" in source
        if (
            discharging
            and battery.percent <= config.battery_alert_percent
            and not battery_active
        ):
            estimate = (
                f" · `{_forecast_duration(battery.estimate_minutes)}`"
                if battery.estimate_minutes is not None
                else ""
            )
            alerts.append(
                Alert(
                    f"system:battery:{metrics.observed_at}",
                    "Battery level is low",
                    (
                        f"`{_progress_bar(battery.percent)}`  **{battery.percent}%**\n"
                        f"{battery.state}{estimate}"
                    ),
                    "critical",
                )
            )
            storage.set_state(battery_active_key, True)
        elif (
            battery.percent >= config.battery_recovery_percent
            or not discharging
        ):
            storage.set_state(battery_active_key, False)
    return tuple(alerts)


def notification_preview_alerts(_resume_message: str) -> tuple[Alert, ...]:
    return (
        Alert(
            "preview:forecast",
            "5-hour limit forecast",
            (
                "## 00:00:24\nEstimated time to reach the 5-hour limit."
                f"{SECTION_SPACER}\n`{_progress_bar(18)}`  **18%**"
            ),
            "warning",
        ),
        Alert(
            "preview:anomaly",
            "Usage rate increased",
            "```\n15 min      0.84%/min\n60 min      0.36%/min\n```",
            "warning",
        ),
        Alert(
            "preview:reached",
            "5-hour limit reached",
            f"`{_progress_bar(0)}`  **0%**\nWaiting for the 5-hour reset.",
            "critical",
        ),
        Alert(
            "preview:reset",
            "5-hour limit reset",
            f"`{_progress_bar(100)}`  **100%**\n5-hour usage is available again.",
            "success",
        ),
        Alert(
            "preview:resumed",
            "Limit reset · Work resumed",
            (
                f"`{_progress_bar(100)}`  **100%**\n"
                "Resume prompt sent to the selected task."
            ),
            "success",
        ),
        Alert(
            "preview:resume-failed",
            "Automatic resume failed",
            "Retry scheduled for the next refresh.\nUse `/health` for diagnostics.",
            "critical",
        ),
        Alert(
            "preview:cpu",
            "CPU pressure is high",
            "`███████████████░`  **94.0%**\nLoad `7.20` / `5.10` / `3.80`.",
            "warning",
        ),
        Alert(
            "preview:memory",
            "Memory pressure is high",
            "`███████████████░`  **92%**\n7.4 GB / 8.0 GB",
            "warning",
        ),
        Alert(
            "preview:battery",
            "Battery level is low",
            "`██░░░░░░░░░░░░░░`  **15%**\nDischarging · `00:01:42`",
            "critical",
        ),
    )


def status_embeds(
    sample: UsageSample,
    forecast: Forecast,
    timezone: ZoneInfo,
    poll_seconds: int = 300,
    battery: Optional[BatteryStatus] = None,
    metrics: Optional[ComputerMetrics] = None,
    computer_use: Optional[ComputerUseStatus] = None,
    footer_icon_url: Optional[str] = None,
) -> tuple[discord.Embed]:
    headline = _forecast_headline(sample, forecast) + SECTION_SPACER
    embed = discord.Embed(
        title="CodexPulse · Usage Monitor",
        description=headline,
        color=_status_color(sample, forecast),
        timestamp=datetime.fromtimestamp(sample.observed_at, timezone),
    )
    embed.add_field(
        name="5-hour limit", value=_primary_limit_text(sample), inline=True
    )

    weekly = "Not reported"
    if sample.secondary_remaining_percent is not None:
        weekly = (
            f"`{_progress_bar(sample.secondary_remaining_percent)}`  "
            f"**{sample.secondary_remaining_percent:.0f}%**\n"
            f"Reset `{_countdown(sample.secondary_resets_at, sample.observed_at)}`"
            f"{SECTION_SPACER}"
        )
    embed.add_field(name="Weekly limit", value=weekly, inline=True)

    pace = (
        "```\n"
        f"{'15 min':<12}{_rate(forecast.rate_15m)}\n"
        f"{'60 min':<12}{_rate(forecast.rate_60m)}\n"
        f"{'24 hours':<12}{_rate(forecast.rate_24h)}\n"
        f"```{SECTION_SPACER}"
    )
    embed.add_field(
        name="Forecast basis",
        value=(
            f"Confidence **{_confidence(forecast.confidence)}** · "
            f"{forecast.sample_count} samples · "
            f"{_history_duration(forecast.span_minutes)} history"
            f"{SECTION_SPACER}"
        ),
        inline=False,
    )
    embed.add_field(name="Consumption rate", value=pace, inline=False)

    embed.add_field(
        name="Account",
        value=f"{_account_text(sample, forecast)}{SECTION_SPACER}",
        inline=False,
    )
    embed.add_field(name="CPU", value=_cpu_text(metrics), inline=True)
    embed.add_field(
        name="Memory", value=_memory_text(metrics), inline=True
    )
    embed.add_field(
        name="Battery", value=_battery_text(battery), inline=False
    )
    interval_minutes = max(1, round(poll_seconds / 60))
    footer_text = (
        f"{_computer_use_footer_text(computer_use, sample.observed_at)} • "
        f"Refresh: {interval_minutes} min"
    )
    if footer_icon_url:
        embed.set_footer(text=footer_text, icon_url=footer_icon_url)
    else:
        embed.set_footer(text=footer_text)
    return (embed,)


def status_embed(
    sample: UsageSample,
    forecast: Forecast,
    timezone: ZoneInfo,
    poll_seconds: int = 300,
    battery: Optional[BatteryStatus] = None,
    metrics: Optional[ComputerMetrics] = None,
    computer_use: Optional[ComputerUseStatus] = None,
    footer_icon_url: Optional[str] = None,
) -> discord.Embed:
    """Return the primary status card for callers using the pre-3.5 API."""
    return status_embeds(
        sample,
        forecast,
        timezone,
        poll_seconds,
        battery,
        metrics,
        computer_use,
        footer_icon_url,
    )[0]


def command_result_embed(
    title: str,
    description: str,
    *,
    severity: str = "info",
) -> discord.Embed:
    return _command_card(title, description=description)


def usage_summary_embed(
    sample: UsageSample,
    timezone: ZoneInfo,
) -> discord.Embed:
    if sample.secondary_exhausted:
        state = "## LIMIT REACHED\nWeekly usage is unavailable until reset."
    elif sample.primary_exhausted:
        state = "## LIMIT REACHED\n5-hour usage is unavailable until reset."
    elif sample.ordinary_usage_allowed is False:
        state = "## LIMIT REACHED\nCodex usage is unavailable until reset."
    else:
        state = "## AVAILABLE\nCodex usage is currently available."
    embed = _command_card(
        "Status",
        description=f"{state}{SECTION_SPACER}",
        timestamp=datetime.fromtimestamp(sample.observed_at, timezone),
    )
    embed.add_field(
        name="5-hour limit",
        value=_primary_limit_text(sample),
        inline=True,
    )
    weekly = f"Not reported{SECTION_SPACER}"
    if sample.secondary_remaining_percent is not None:
        weekly = (
            f"`{_progress_bar(sample.secondary_remaining_percent)}`  "
            f"**{sample.secondary_remaining_percent:.0f}%**\n"
            f"Reset `{_countdown(sample.secondary_resets_at, sample.observed_at)}`"
            f"{SECTION_SPACER}"
        )
    embed.add_field(name="Weekly limit", value=weekly, inline=True)
    embed.add_field(name="Account", value=_account_text(sample), inline=False)
    _set_command_footer(embed, "Limits only", "/forecast for prediction")
    return embed


def forecast_only_embed(
    sample: UsageSample,
    forecast: Forecast,
    timezone: ZoneInfo,
) -> discord.Embed:
    estimate = _forecast_headline(sample, forecast)
    embed = _command_card(
        "Forecast",
        description=f"{estimate}{SECTION_SPACER}",
        timestamp=datetime.fromtimestamp(sample.observed_at, timezone),
    )
    embed.add_field(
        name="Forecast basis",
        value=(
            f"Confidence **{_confidence(forecast.confidence)}** · "
            f"{forecast.sample_count} samples · "
            f"{_history_duration(forecast.span_minutes)} history"
            f"{SECTION_SPACER}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Consumption rate",
        value=(
            "```\n"
            f"{'15 min':<12}{_rate(forecast.rate_15m)}\n"
            f"{'60 min':<12}{_rate(forecast.rate_60m)}\n"
            f"{'24 hours':<12}{_rate(forecast.rate_24h)}\n"
            "```"
        ),
        inline=False,
    )
    _set_command_footer(embed, "Prediction only", "/status for limits")
    return embed


def history_embed(samples: list[UsageSample], timezone: ZoneInfo) -> discord.Embed:
    selected = samples[-40:]
    latest = samples[-1]
    earliest = samples[0]
    use_weekly = not latest.primary_limit_available
    if use_weekly:
        values = [
            sample.secondary_used_percent
            for sample in selected
            if sample.secondary_used_percent is not None
        ]
        current = latest.secondary_remaining_percent
        earliest_used = earliest.secondary_used_percent
        latest_used = latest.secondary_used_percent
        window_label = "Weekly"
    else:
        values = [sample.primary_used_percent for sample in selected]
        current = latest.primary_remaining_percent
        earliest_used = earliest.primary_used_percent
        latest_used = latest.primary_used_percent
        window_label = "5-hour"
    delta = (
        latest_used - earliest_used
        if latest_used is not None and earliest_used is not None
        else 0.0
    )
    embed = _command_card(
        "24-hour History",
        timestamp=datetime.fromtimestamp(latest.observed_at, timezone),
    )
    embed.add_field(
        name="Usage trend",
        value=f"`{_sparkline(values)}`{SECTION_SPACER}",
        inline=False,
    )
    embed.add_field(
        name="Current",
        value=(
            f"**{current:.1f}%** · {window_label}"
            if current is not None
            else "Not reported"
        ),
        inline=True,
    )
    embed.add_field(name="Change", value=f"**{delta:+.1f}** points", inline=True)
    embed.add_field(name="Samples", value=f"**{len(samples)}**", inline=True)
    return embed


def health_embed(
    *,
    success_at: Optional[int],
    wake_at: Optional[int],
    analytics_at: Optional[int],
    interval_seconds: int,
    queue_count: int,
    auto_resume: bool,
    last_error: Optional[str],
    resume_error: Optional[str],
    analytics_error: Optional[str],
    timezone: ZoneInfo,
    database_bytes: int = 0,
    log_bytes: int = 0,
    retention_days: int = 90,
    maintenance_at: Optional[int] = None,
    storage_error: Optional[str] = None,
) -> discord.Embed:
    errors = [
        value
        for value in (last_error, resume_error, analytics_error, storage_error)
        if value
    ]
    health_state = "ATTENTION" if errors else "HEALTHY"
    health_detail = "Review diagnostics below." if errors else "All services are operating normally."
    embed = _command_card(
        "Health",
        description=f"## {health_state}\n{health_detail}{SECTION_SPACER}",
        timestamp=datetime.now(timezone),
    )
    embed.add_field(
        name="Service",
        value=(
            "**Online**\n"
            f"Last update `{_local_time(success_at, timezone)}`\n"
            f"Refresh: `{max(1, interval_seconds // 60)} min`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Analytics",
        value=(
            f"**{'Attention' if analytics_error else 'Healthy'}**\n"
            f"Last import `{_local_time(analytics_at, timezone)}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Resume",
        value=(
            f"Auto resume **{'On' if auto_resume else 'Off'}**\n"
            f"Queue `{queue_count}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Storage",
        value=(
            f"Database: {_size(database_bytes)} · Log: {_size(log_bytes)}\n"
            f"Retention: {retention_days} days · "
            f"Last cleanup: {_local_time(maintenance_at, timezone)}"
        ),
        inline=False,
    )
    if wake_at:
        embed.add_field(
            name="System wake",
            value=_local_time(wake_at, timezone),
            inline=False,
        )
    if errors:
        labels = (
            ("Monitor", last_error),
            ("Resume", resume_error),
            ("Analytics", analytics_error),
            ("Storage", storage_error),
        )
        diagnostics = "\n".join(
            f"**{label}:** `{str(value)[:350]}`"
            for label, value in labels
            if value
        )
        embed.add_field(name="Diagnostics", value=diagnostics, inline=False)
    else:
        embed.add_field(name="Diagnostics", value="No active errors.", inline=False)
    return embed


def _sparkline(values: list[float]) -> str:
    if not values:
        return "—"
    bars = "▁▂▃▄▅▆▇█"
    low, high = min(values), max(values)
    if high == low:
        return bars[0] * len(values)
    return "".join(
        bars[min(7, int((value - low) / (high - low) * 7))] for value in values
    )


def _tokens(value: float | int) -> str:
    amount = float(value)
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}k"
    return f"{amount:.0f}"


def _limit_rate(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.3f}% / active min"


def model_usage_embed(
    profiles: list[dict], days: int, *, title: str = "Model & Reasoning Profiler"
) -> discord.Embed:
    embed = _command_card(title)
    if not profiles:
        embed.description = "No completed turn telemetry is available yet."
        return embed
    embed.description = (
        f"Last {days} days · limit attribution is estimated when tasks overlap."
        f"{SECTION_SPACER}"
    )
    for profile in profiles[:10]:
        rate = _limit_rate(profile.get("percent_per_active_min"))
        cache = profile.get("cache_percent")
        cache_text = "—" if cache is None else f"{cache:.0f}%"
        embed.add_field(
            name=f"{profile['model']} · {profile['effort']}",
            value=(
                f"{rate} · {profile['percent_per_turn']:.2f}% / turn\n"
                f"{_tokens(profile['reasoning_per_turn'])} reasoning / turn · "
                f"{cache_text} cached\n"
                f"{profile['sample_count']} turns · "
                f"Confidence **{str(profile['confidence']).title()}**"
            ),
            inline=False,
        )
    return embed


def project_usage_embed(profiles: list[dict], days: int) -> discord.Embed:
    embed = _command_card("Project Burn Rate")
    if not profiles:
        embed.description = "No project telemetry is available yet."
        return embed
    total = sum(float(profile["attributed_percent"]) for profile in profiles)
    embed.description = f"Last {days} days · {len(profiles)} projects{SECTION_SPACER}"
    for profile in profiles[:12]:
        share = (
            float(profile["attributed_percent"]) / total * 100 if total > 0 else 0
        )
        embed.add_field(
            name=str(profile["project_name"])[:256],
            value=(
                f"{profile['attributed_percent']:.1f}% attributed · {share:.0f}% share\n"
                f"{profile['sample_count']} turns · "
                f"{_limit_rate(profile.get('percent_per_active_min'))}"
            ),
            inline=False,
        )
    return embed


def consumers_embed(rows: list[dict], hours: int, title: str = "What used my limit?") -> discord.Embed:
    embed = _command_card(title)
    if not rows:
        embed.description = "No completed turns are available for this period."
        return embed
    embed.description = (
        f"Last {hours} hours · ranked by attributed limit usage{SECTION_SPACER}"
    )
    for index, row in enumerate(rows[:8], 1):
        attributed = float(row.get("attributed_percent") or 0)
        embed.add_field(
            name=f"{index}. {row.get('project_name') or 'Unknown'}",
            value=(
                f"{row.get('model')} · {row.get('effort')} · "
                f"{attributed:.2f}% attributed\n"
                f"{_tokens(int(row.get('total_tokens') or 0))} tokens · "
                f"{_tokens(int(row.get('reasoning_output_tokens') or 0))} reasoning · "
                "Confidence "
                f"**{str(row.get('attribution_confidence') or 'unavailable').title()}**"
            ),
            inline=False,
        )
    return embed


def efficiency_embed(rows: list[dict], days: int) -> discord.Embed:
    embed = _command_card("Context Efficiency")
    if not rows:
        embed.description = (
            f"No unusual context or reasoning usage found in the last {days} days."
        )
        return embed
    embed.description = (
        "Advisory only · compared with similar turns in the same project."
        f"{SECTION_SPACER}"
    )
    for row in rows[:8]:
        embed.add_field(
            name=f"{row.get('project_name')} · {row.get('model')} · {row.get('effort')}",
            value=(
                f"{' · '.join(str(value) for value in row['reasons'])}\n"
                f"{_tokens(int(row.get('max_request_input_tokens') or 0))} max context · "
                f"{_tokens(int(row.get('reasoning_output_tokens') or 0))} reasoning"
            ),
            inline=False,
        )
    return embed


def accuracy_embed(value: dict, days: int) -> discord.Embed:
    embed = _command_card("Forecast Accuracy")
    if value["exhaustion_count"] == 0:
        embed.description = (
            f"Last {days} days · no completed exhaustion event is available yet.\n"
            f"{value['prediction_count']} resolved predictions · "
            f"{value['reset_first_count']} reset before exhaustion"
        )
        return embed
    bias = float(value["median_bias_minutes"])
    bias_text = f"{abs(bias):.0f} min {'late' if bias > 0 else 'early'}"
    embed.add_field(
        name="Median error",
        value=f"{value['median_absolute_error_minutes']:.0f} min",
        inline=True,
    )
    embed.add_field(
        name="Within ±15 min",
        value=f"{value['within_15_minutes_percent']:.0f}%",
        inline=True,
    )
    embed.add_field(name="Bias", value=bias_text, inline=True)
    _set_command_footer(
        embed,
        f"Observed data: {days} days",
        f"{value['exhaustion_count']} exhaustion events",
    )
    return embed


def advisor_embed(value: dict, remaining_percent: float, days: int) -> discord.Embed:
    embed = _command_card("Limit-aware Advisor")
    embed.description = (
        "Advisory only · CodexPulse never changes the model or reasoning effort."
        f"{SECTION_SPACER}"
    )
    alternatives = value.get("alternatives", [])
    if not alternatives:
        embed.add_field(
            name="Data still learning",
            value="More completed turns are needed before a reliable comparison can be made.",
            inline=False,
        )
        return embed
    current = value.get("current")
    if current is not None:
        current_minutes = remaining_percent / float(current["percent_per_active_min"])
        embed.add_field(
            name="Current observed configuration",
            value=(
                f"**{current['model']} · {current['effort']}**\n"
                f"Estimated {_forecast_duration(current_minutes)} of active work · "
                f"{current['sample_count']} turns"
            ),
            inline=False,
        )
    for profile in alternatives[:5]:
        embed.add_field(
            name=f"{profile['model']} · {profile['effort']}",
            value=(
                f"Estimated **{_forecast_duration(profile['estimated_minutes'])}** active work · "
                f"{_limit_rate(profile['percent_per_active_min'])}\n"
                f"{profile['sample_count']} turns · Confidence "
                f"**{str(profile['confidence']).title()}**"
            ),
            inline=False,
        )
    _set_command_footer(
        embed, f"Observed data: {days} days", "No settings changed"
    )
    return embed


def resume_queue_embed(targets: list) -> discord.Embed:
    embed = _command_card("Resume Queue")
    if not targets:
        embed.description = "No tasks are waiting after a usage-limit stop."
        return embed
    embed.description = (
        "Only the latest task is resumed automatically. Additional tasks require an explicit command."
        f"{SECTION_SPACER}"
    )
    for index, target in enumerate(targets[:10], 1):
        project = f" · {target.project_name}" if target.project_name else ""
        embed.add_field(
            name=f"{index}. {target.label[:220]}",
            value=f"Stopped <t:{target.stopped_at}:R>{project}",
            inline=False,
        )
    return embed


def weekly_digest_embed(storage: Storage, now: int, days: int = 7) -> discord.Embed:
    profiles = model_profiles(storage, now - days * 86400)
    projects = project_profiles(storage, now - days * 86400)
    accuracy = forecast_accuracy(storage, now - 30 * 86400)
    embed = _command_card(
        "Weekly Digest",
        timestamp=datetime.fromtimestamp(now),
    )
    if profiles:
        top = profiles[0]
        embed.add_field(
            name="Top model · effort",
            value=(
                f"**{top['model']} · {top['effort']}**\n"
                f"{top['attributed_percent']:.1f}% attributed · {top['sample_count']} turns"
            ),
            inline=False,
        )
    if projects:
        top_project = projects[0]
        embed.add_field(
            name="Top project",
            value=(
                f"**{top_project['project_name']}** · "
                f"{top_project['attributed_percent']:.1f}% attributed"
            ),
            inline=False,
        )
    embed.add_field(
        name="Forecast",
        value=(
            f"{accuracy['exhaustion_count']} measured exhaustion events · "
            + (
                f"median error {accuracy['median_absolute_error_minutes']:.0f} min"
                if accuracy["median_absolute_error_minutes"] is not None
                else "accuracy still learning"
            )
        ),
        inline=False,
    )
    if not profiles and not projects:
        embed.description = "Telemetry is still being collected."
    _set_command_footer(embed, "Observed data only", "No settings changed")
    return embed


def help_embed() -> discord.Embed:
    embed = _command_card(
        "Command Guide",
        description=(
            "Private owner commands · optional periods use the defaults shown."
            f"{SECTION_SPACER}"
        ),
    )
    embed.add_field(
        name="Usage",
        value=(
            "`/status` — limit balances and availability\n"
            "`/forecast` — exhaustion estimate and consumption speed\n"
            "`/history` — 24-hour usage trend\n"
            "`/refresh` — collect and update now"
        ),
        inline=False,
    )
    embed.add_field(
        name="Analysis",
        value=(
            "`/model-usage [days:7]` — model × reasoning profile\n"
            "`/project-usage [days:7]` — usage by project\n"
            "`/why [hours:24]` — highest recent consumers\n"
            "`/turn-details [position:1]` — token details for `/why`\n"
            "`/efficiency [days:7]` — context and reasoning anomalies\n"
            "`/forecast-accuracy [days:30]` — prediction accuracy"
        ),
        inline=False,
    )
    embed.add_field(
        name="Advisory",
        value=(
            "`/recommend [days:14]` — observed efficient configurations\n"
            "`/remaining-by-model [days:14]` — remaining time comparison\n"
            "`/simulate model effort [days:14]` — configuration scenario\n"
            "`/compare … [days:14]` — compare two configurations"
        ),
        inline=False,
    )
    embed.add_field(
        name="Resume & notifications",
        value=(
            "`/resume-queue` — tasks stopped by the usage limit\n"
            "`/resume-next` — explicitly resume the next task\n"
            "`/resume-select position` — resume a selected task\n"
            "`/notify enabled` — enable or disable important alerts\n"
            "`/test-notification` — preview all alerts after 10 seconds\n"
            "`/digest` — preview the weekly digest"
        ),
        inline=False,
    )
    embed.add_field(
        name="System",
        value=(
            "`/health` — monitor, analytics, wake and queue health\n"
            "`/computer` — live computer resource and battery status\n"
            "`/screenshot` — privately capture every active display\n"
            "`/help` — show this guide"
        ),
        inline=False,
    )
    _set_command_footer(embed, "No settings changed")
    return embed


def computer_embed(metrics: ComputerMetrics, timezone: ZoneInfo) -> discord.Embed:
    disk = (
        f"`{_progress_bar(metrics.disk_used_percent)}`  "
        f"**{metrics.disk_used_percent:.0f}%**\n"
        f"`{_size(metrics.disk_used_bytes)}` / `{_size(metrics.disk_total_bytes)}`"
    )
    embed = _command_card(
        "Computer",
        timestamp=datetime.fromtimestamp(metrics.observed_at, timezone),
    )
    embed.add_field(name="CPU", value=_cpu_text(metrics), inline=True)
    embed.add_field(name="Memory", value=_memory_text(metrics), inline=True)
    embed.add_field(
        name="Storage", value=f"{disk}{SECTION_SPACER}", inline=False
    )
    embed.add_field(name="Battery", value=_battery_text(metrics.battery), inline=False)
    embed.add_field(
        name="System",
        value=(
            f"{metrics.architecture} · {metrics.cpu_count} logical CPUs · "
            f"Active uptime `{_forecast_duration(metrics.active_uptime_seconds / 60)}`"
        ),
        inline=False,
    )
    return embed


def screenshot_embed(
    filename: str,
    display_number: int,
    display_count: int,
    timezone: ZoneInfo,
    delete_seconds: int,
) -> discord.Embed:
    embed = _command_card(
        "Screenshot",
        description=(
            "Full display captured now."
            if display_count == 1
            else f"Display {display_number} of {display_count} captured now."
        ),
        timestamp=datetime.now(timezone),
    )
    embed.set_image(url=f"attachment://{filename}")
    _set_command_footer(
        embed, "Owner only", f"Auto-delete: {delete_seconds} sec"
    )
    return embed


class MonitorBot(discord.Client):
    def __init__(
        self, config: AppConfig, storage: Storage, service: MonitorService
    ) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.config = config
        self.storage = storage
        self.service = service
        self.tree = app_commands.CommandTree(self)
        self.timezone = ZoneInfo(config.monitor.timezone)
        self.refresh_lock = asyncio.Lock()
        self.background_tasks: set[asyncio.Task[None]] = set()
        self.last_result: Optional[CycleResult] = None
        self.last_error: Optional[str] = None
        self._notification_recovery_complete = False
        self._wake_wall_time = time.time()
        self._wake_monotonic_time = time.monotonic()
        self.monitor_loop.change_interval(seconds=config.monitor.poll_seconds)
        self._register_commands()

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.monitor_loop.start()
        self.wake_watch_loop.start()
        self.notification_cleanup_loop.start()
        if self.config.analytics.weekly_digest_enabled:
            self.weekly_digest_loop.start()

    async def close(self) -> None:
        self.monitor_loop.cancel()
        self.wake_watch_loop.cancel()
        self.notification_cleanup_loop.cancel()
        self.weekly_digest_loop.cancel()
        for task in self.background_tasks:
            task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        await super().close()

    async def _channel(self):
        channel = self.get_channel(self.config.discord.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.discord.channel_id)
        if not hasattr(channel, "send"):
            raise RuntimeError("Configured Discord channel cannot receive messages")
        return channel

    async def _send_transient_notification(
        self,
        channel: Any,
        *,
        delete_seconds: int,
        **kwargs: Any,
    ) -> discord.Message:
        seconds = max(1, int(delete_seconds))
        message = await channel.send(delete_after=seconds, **kwargs)
        self.storage.schedule_discord_cleanup(
            message.id,
            channel.id,
            int(time.time()) + seconds,
        )
        return message

    async def _recover_transient_notifications(self) -> None:
        if self._notification_recovery_complete:
            return
        channel = await self._channel()
        if not hasattr(channel, "history"):
            LOGGER.warning("Configured Discord channel does not expose message history")
            return
        try:
            async for message in channel.history(
                limit=NOTIFICATION_RECOVERY_HISTORY_LIMIT
            ):
                if self.user is None or message.author.id != self.user.id:
                    continue
                ttl_seconds = _notification_ttl_seconds(message)
                if ttl_seconds is None:
                    continue
                self.storage.schedule_discord_cleanup(
                    message.id,
                    channel.id,
                    int(message.created_at.timestamp()) + ttl_seconds,
                )
        except discord.Forbidden:
            LOGGER.warning(
                "Bot cannot recover transient notifications without message history permission"
            )
            return
        except discord.HTTPException:
            LOGGER.exception("Transient notification recovery failed")
            return
        self._notification_recovery_complete = True

    async def _delete_due_notifications(self) -> None:
        for pending in self.storage.due_discord_cleanups():
            message_id = pending["message_id"]
            channel_id = pending["channel_id"]
            try:
                channel = self.get_channel(channel_id)
                if channel is None:
                    channel = await self.fetch_channel(channel_id)
                if not hasattr(channel, "fetch_message"):
                    raise RuntimeError(
                        f"Discord channel {channel_id} cannot fetch messages"
                    )
                message = await channel.fetch_message(message_id)
                await message.delete()
            except discord.NotFound:
                self.storage.complete_discord_cleanup(message_id)
            except discord.Forbidden:
                LOGGER.warning(
                    "Bot cannot delete transient Discord message %s", message_id
                )
            except discord.HTTPException:
                LOGGER.exception(
                    "Transient Discord message cleanup failed for %s", message_id
                )
            except Exception:
                LOGGER.exception(
                    "Unexpected transient Discord message cleanup failure for %s",
                    message_id,
                )
            else:
                self.storage.complete_discord_cleanup(message_id)

    async def _update_status(self, result: CycleResult) -> Optional[ComputerMetrics]:
        channel = await self._channel()
        metrics: Optional[ComputerMetrics] = None
        try:
            metrics = await asyncio.to_thread(collect_computer_metrics)
        except Exception:
            LOGGER.exception("Periodic computer metrics collection failed")
        battery = (
            metrics.battery
            if metrics is not None
            else await asyncio.to_thread(collect_battery_status)
        )
        computer_use = await asyncio.to_thread(
            detect_computer_use,
            self.config.analytics.sessions_dir,
            result.sample.observed_at,
        )
        message_id = self.storage.get_state("discord_status_message_id")
        message = None
        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                message = None
        footer_icon_url = (
            str(self.user.display_avatar.url) if self.user is not None else None
        )
        embeds = status_embeds(
            result.sample,
            result.forecast,
            self.timezone,
            self.config.monitor.poll_seconds,
            battery=battery,
            metrics=metrics,
            computer_use=computer_use,
            footer_icon_url=footer_icon_url,
        )
        if message is None:
            message = await channel.send(embeds=list(embeds))
            self.storage.set_state("discord_status_message_id", message.id)
            if self.config.discord.pin_status_message:
                try:
                    await message.pin(reason="Codex usage status")
                except discord.Forbidden:
                    LOGGER.warning("Bot cannot pin the status message")
        else:
            await message.edit(embeds=list(embeds), attachments=[], view=None)
        return metrics

    async def _publish_weekly_digest(self, now: Optional[int] = None) -> None:
        timestamp = int(time.time()) if now is None else int(now)
        channel = await self._channel()
        embed = weekly_digest_embed(self.storage, timestamp)
        message_id = self.storage.get_state("discord_weekly_digest_message_id")
        message = None
        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(embed=embed)
            self.storage.set_state("discord_weekly_digest_message_id", message.id)
        else:
            await message.edit(embed=embed)
        if self.config.analytics.weekly_digest_mention:
            mention_id = (
                self.config.discord.mention_user_id
                or self.config.discord.owner_user_id
            )
            await self._send_transient_notification(
                channel,
                delete_seconds=self.config.discord.alert_delete_seconds,
                content=f"<@{mention_id}>",
                embed=alert_embed(
                    Alert(
                        f"weekly-digest-notice:{timestamp}",
                        "Weekly digest updated",
                        "The latest 7-day summary is ready.",
                        "success",
                    ),
                    self.timezone,
                    self.config.discord.alert_delete_seconds,
                    footer_icon_url=(
                        str(self.user.display_avatar.url)
                        if self.user is not None
                        else None
                    ),
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )

    async def _send_alerts(
        self, result: CycleResult, extra_alerts: tuple[Alert, ...] = ()
    ) -> None:
        if not self.storage.get_state("alerts_enabled", True):
            return
        channel = await self._channel()
        mention_id = (
            self.config.discord.mention_user_id
            or self.config.discord.owner_user_id
        )
        for alert in (*result.alerts, *extra_alerts):
            if self.storage.has_event(alert.event_key):
                continue
            await self._send_transient_notification(
                channel,
                delete_seconds=self.config.discord.alert_delete_seconds,
                content=f"<@{mention_id}>",
                embed=alert_embed(
                    alert,
                    self.timezone,
                    self.config.discord.alert_delete_seconds,
                    footer_icon_url=(
                        str(self.user.display_avatar.url)
                        if self.user is not None
                        else None
                    ),
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
            self.storage.mark_event(alert.event_key, {"title": alert.title})
            for event_key in alert.related_event_keys:
                self.storage.mark_event(event_key, {"covered_by": alert.event_key})

    async def _send_delayed_test_notification(self, mention_id: int) -> None:
        try:
            await asyncio.sleep(TEST_NOTIFICATION_DELAY_SECONDS)
            channel = await self._channel()
            preview_alerts = notification_preview_alerts(
                self.config.codex.resume_message
            )
            embeds = [
                alert_embed(
                    alert,
                    self.timezone,
                    self.config.discord.alert_delete_seconds,
                    footer_icon_url=(
                        str(self.user.display_avatar.url)
                        if self.user is not None
                        else None
                    ),
                )
                for alert in preview_alerts
            ]
            await self._send_transient_notification(
                channel,
                delete_seconds=self.config.discord.alert_delete_seconds,
                content=f"<@{mention_id}>",
                embeds=embeds,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Delayed notification test failed")

    async def refresh(self) -> CycleResult:
        async with self.refresh_lock:
            try:
                result = await asyncio.to_thread(self.service.run_cycle)
                metrics = await self._update_status(result)
                extra_alerts = ()
                if (
                    metrics is not None
                    and self.storage.get_state("alerts_enabled", True)
                ):
                    extra_alerts = system_metric_alerts(
                        metrics,
                        self.storage,
                        self.config.monitor,
                    )
                await self._send_alerts(result, extra_alerts)
                self.last_result = result
                self.last_error = None
                self.storage.set_state("last_success_at", int(time.time()))
                self.storage.set_state("last_error", None)
                return result
            except Exception as error:
                self.last_error = f"{type(error).__name__}: {error}"
                self.storage.set_state("last_error", self.last_error)
                LOGGER.exception("Monitor refresh failed")
                raise

    @tasks.loop(seconds=300)
    async def monitor_loop(self) -> None:
        try:
            await self.refresh()
        except Exception:
            pass

    @monitor_loop.before_loop
    async def before_monitor_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=WAKE_CHECK_SECONDS)
    async def wake_watch_loop(self) -> None:
        wall_now = time.time()
        monotonic_now = time.monotonic()
        woke_from_sleep = _system_sleep_detected(
            self._wake_wall_time,
            self._wake_monotonic_time,
            wall_now,
            monotonic_now,
        )
        self._wake_wall_time = wall_now
        self._wake_monotonic_time = monotonic_now
        if not woke_from_sleep:
            return
        self.storage.set_state("last_wake_at", int(wall_now))
        LOGGER.info("System wake detected; refreshing Codex usage")
        await asyncio.sleep(5)
        try:
            await self.refresh()
        except Exception:
            pass

    @wake_watch_loop.before_loop
    async def before_wake_watch_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=NOTIFICATION_CLEANUP_SECONDS)
    async def notification_cleanup_loop(self) -> None:
        try:
            await self._recover_transient_notifications()
            await self._delete_due_notifications()
        except Exception:
            LOGGER.exception("Notification cleanup cycle failed")

    @notification_cleanup_loop.before_loop
    async def before_notification_cleanup_loop(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=15)
    async def weekly_digest_loop(self) -> None:
        now = datetime.now(self.timezone)
        if (
            now.weekday() != self.config.analytics.weekly_digest_weekday
            or now.hour != self.config.analytics.weekly_digest_hour
        ):
            return
        iso_year, iso_week, _ = now.isocalendar()
        event_key = f"weekly-digest:{iso_year}:{iso_week}"
        if self.storage.has_event(event_key):
            return
        try:
            await self._publish_weekly_digest(int(now.timestamp()))
            self.storage.mark_event(event_key, {"at": int(now.timestamp())})
        except Exception:
            LOGGER.exception("Weekly digest update failed")

    @weekly_digest_loop.before_loop
    async def before_weekly_digest_loop(self) -> None:
        await self.wait_until_ready()

    async def _authorized(self, interaction: discord.Interaction) -> bool:
        task = asyncio.create_task(
            self._delete_command_response_later(interaction),
            name="codex-pulse-command-cleanup",
        )
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        if interaction.user.id == self.config.discord.owner_user_id:
            return True
        await interaction.response.send_message(
            embed=command_result_embed(
                "Access denied",
                "Only the configured owner can use this command.",
                severity="error",
            ),
            ephemeral=True,
        )
        return False

    async def _delete_command_response_later(
        self, interaction: discord.Interaction
    ) -> None:
        await asyncio.sleep(self._command_response_delete_seconds())
        try:
            await interaction.delete_original_response()
        except (discord.NotFound, discord.HTTPException):
            pass

    def _command_response_delete_seconds(self) -> int:
        return max(
            1,
            self.config.discord.command_delete_seconds,
            self.config.discord.alert_delete_seconds + 60,
        )

    def _register_commands(self) -> None:
        @self.tree.command(name="help", description="Show the CodexPulse command guide")
        async def help_command(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await interaction.response.send_message(
                embed=help_embed(),
                ephemeral=True,
            )

        @self.tree.command(name="status", description="Show current Codex usage")
        async def status(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Status", "No usage data is available yet.", severity="warning"
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=usage_summary_embed(sample, self.timezone),
                ephemeral=True,
            )

        @self.tree.command(name="forecast", description="Show the usage forecast")
        async def forecast_command(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Forecast", "No usage data is available yet.", severity="warning"
                    ),
                    ephemeral=True,
                )
                return
            history = self.storage.recent_samples(sample.observed_at - 7 * 86400)
            from .forecast import forecast_usage

            value = forecast_usage(history, self.config.monitor)
            await interaction.response.send_message(
                embed=forecast_only_embed(sample, value, self.timezone),
                ephemeral=True,
            )

        @self.tree.command(name="history", description="Show the last 24 hours")
        async def history(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            now = int(time.time())
            samples = self.storage.recent_samples(now - 86400)
            if not samples:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "24-hour History",
                        "No usage data is available yet.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=history_embed(samples, self.timezone), ephemeral=True
            )

        @self.tree.command(name="refresh", description="Refresh usage now")
        async def refresh(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self.refresh()
                if result.sample.primary_limit_available:
                    detail = (
                        f"**{result.sample.primary_remaining_percent:.0f}%** "
                        "in the 5-hour limit."
                    )
                elif result.sample.secondary_remaining_percent is not None:
                    detail = (
                        "No 5-hour limit · "
                        f"**{result.sample.secondary_remaining_percent:.0f}%** "
                        "in the weekly limit."
                    )
                else:
                    detail = "No usage limit is reported for this plan."
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Updated",
                        detail,
                        severity="success",
                    ),
                )
            except Exception as error:
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Refresh failed", str(error)[:500], severity="error"
                    ),
                )

        @self.tree.command(name="notify", description="Enable or disable alerts")
        @app_commands.describe(enabled="Enable alerts")
        async def notify(interaction: discord.Interaction, enabled: bool) -> None:
            if not await self._authorized(interaction):
                return
            self.storage.set_state("alerts_enabled", enabled)
            await interaction.response.send_message(
                embed=command_result_embed(
                    "Notifications",
                    f"Important alerts are now **{'enabled' if enabled else 'disabled'}**.",
                    severity="success" if enabled else "warning",
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="model-usage",
            description="Compare observed usage by model and reasoning effort",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def model_usage(interaction: discord.Interaction, days: int = 7) -> None:
            if not await self._authorized(interaction):
                return
            days = max(1, min(90, days))
            values = model_profiles(self.storage, int(time.time()) - days * 86400)
            await interaction.response.send_message(
                embed=model_usage_embed(values, days), ephemeral=True
            )

        @self.tree.command(
            name="project-usage",
            description="Show attributed usage by project",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def project_usage(interaction: discord.Interaction, days: int = 7) -> None:
            if not await self._authorized(interaction):
                return
            days = max(1, min(90, days))
            values = project_profiles(self.storage, int(time.time()) - days * 86400)
            await interaction.response.send_message(
                embed=project_usage_embed(values, days), ephemeral=True
            )

        @self.tree.command(
            name="why",
            description="Explain which recent turns consumed the limit",
        )
        @app_commands.describe(hours="Analysis period from 1 to 168 hours")
        async def why(interaction: discord.Interaction, hours: int = 24) -> None:
            if not await self._authorized(interaction):
                return
            hours = max(1, min(168, hours))
            values = recent_consumers(
                self.storage, int(time.time()) - hours * 3600
            )
            await interaction.response.send_message(
                embed=consumers_embed(values, hours), ephemeral=True
            )

        @self.tree.command(
            name="turn-details",
            description="Show details for a recent high-consumption turn",
        )
        @app_commands.describe(position="Position from /why, starting at 1")
        async def turn_details(
            interaction: discord.Interaction, position: int = 1
        ) -> None:
            if not await self._authorized(interaction):
                return
            values = recent_consumers(self.storage, int(time.time()) - 7 * 86400, 25)
            if position < 1 or position > len(values):
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Turn Details",
                        "No recent turn exists at that position.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            row = values[position - 1]
            input_tokens = int(row.get("input_tokens") or 0)
            cached = int(row.get("cached_input_tokens") or 0)
            cache_percent = cached / input_tokens * 100 if input_tokens else 0
            embed = _command_card("Turn Details")
            embed.add_field(
                name="Configuration",
                value=(
                    f"**{row.get('model')} · {row.get('effort')}**\n"
                    f"{row.get('project_name')}{SECTION_SPACER}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Tokens",
                value=(
                    f"Input {_tokens(input_tokens)} · cached {cache_percent:.0f}%\n"
                    f"Output {_tokens(int(row.get('output_tokens') or 0))} · "
                    f"reasoning {_tokens(int(row.get('reasoning_output_tokens') or 0))}\n"
                    f"Max request context {_tokens(int(row.get('max_request_input_tokens') or 0))}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Limit attribution",
                value=(
                    f"{float(row.get('attributed_percent') or 0):.2f}% · "
                    "Confidence "
                    f"**{str(row.get('attribution_confidence')).title()}**"
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(
            name="efficiency",
            description="Find unusual context and reasoning usage",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def efficiency(interaction: discord.Interaction, days: int = 7) -> None:
            if not await self._authorized(interaction):
                return
            days = max(1, min(90, days))
            values = efficiency_findings(
                self.storage, int(time.time()) - days * 86400
            )
            await interaction.response.send_message(
                embed=efficiency_embed(values, days), ephemeral=True
            )

        @self.tree.command(
            name="forecast-accuracy",
            description="Evaluate previous limit forecasts",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def forecast_accuracy_command(
            interaction: discord.Interaction, days: int = 30
        ) -> None:
            if not await self._authorized(interaction):
                return
            days = max(1, min(90, days))
            value = forecast_accuracy(
                self.storage, int(time.time()) - days * 86400
            )
            await interaction.response.send_message(
                embed=accuracy_embed(value, days), ephemeral=True
            )

        @self.tree.command(
            name="recommend",
            description="Suggest efficient observed configurations without changing them",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def recommend(interaction: discord.Interaction, days: int = 14) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Advisor", "No usage data is available yet.", severity="warning"
                    ),
                    ephemeral=True,
                )
                return
            days = max(1, min(90, days))
            value = advisor(
                self.storage,
                int(time.time()) - days * 86400,
                sample.primary_remaining_percent,
                self.config.analytics.minimum_profile_samples,
            )
            await interaction.response.send_message(
                embed=advisor_embed(value, sample.primary_remaining_percent, days),
                ephemeral=True,
            )

        @self.tree.command(
            name="simulate",
            description="Estimate active work remaining for an observed configuration",
        )
        @app_commands.describe(
            model="Exact model name from /model-usage",
            effort="Reasoning effort such as high or xhigh",
            days="Analysis period from 1 to 90 days",
        )
        async def simulate_command(
            interaction: discord.Interaction,
            model: str,
            effort: str,
            days: int = 14,
        ) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Scenario", "No usage data is available yet.", severity="warning"
                    ),
                    ephemeral=True,
                )
                return
            days = max(1, min(90, days))
            value = simulate(
                self.storage,
                int(time.time()) - days * 86400,
                model,
                effort,
                sample.primary_remaining_percent,
            )
            if value is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Scenario unavailable",
                        "No matching observed configuration. Use `/model-usage` first.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            embed = _command_card("Scenario", description=(
                f"**{value['model']} · {value['effort']}**\n"
                f"Estimated active work: **{_forecast_duration(value['estimated_minutes'])}**\n"
                f"{_limit_rate(value['percent_per_active_min'])} · "
                f"{value['sample_count']} turns · Confidence "
                f"**{str(value['confidence']).title()}**"
            ))
            _set_command_footer(embed, "Advisory only", "No settings changed")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(
            name="remaining-by-model",
            description="Compare estimated active time across observed configurations",
        )
        @app_commands.describe(days="Analysis period from 1 to 90 days")
        async def remaining_by_model(
            interaction: discord.Interaction, days: int = 14
        ) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Remaining by Model",
                        "No usage data is available yet.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            days = max(1, min(90, days))
            value = advisor(
                self.storage,
                int(time.time()) - days * 86400,
                sample.primary_remaining_percent,
                self.config.analytics.minimum_profile_samples,
            )
            embed = advisor_embed(value, sample.primary_remaining_percent, days)
            embed.title = "CodexPulse · Remaining by Model"
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(
            name="compare",
            description="Compare two observed model and effort combinations",
        )
        async def compare(
            interaction: discord.Interaction,
            first_model: str,
            first_effort: str,
            second_model: str,
            second_effort: str,
            days: int = 14,
        ) -> None:
            if not await self._authorized(interaction):
                return
            sample = self.storage.latest_sample()
            if sample is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Configuration Comparison",
                        "No usage data is available yet.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            days = max(1, min(90, days))
            since = int(time.time()) - days * 86400
            first = simulate(
                self.storage,
                since,
                first_model,
                first_effort,
                sample.primary_remaining_percent,
            )
            second = simulate(
                self.storage,
                since,
                second_model,
                second_effort,
                sample.primary_remaining_percent,
            )
            if first is None or second is None:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Comparison unavailable",
                        "One or both configurations have no observed data. Use `/model-usage` first.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            embed = model_usage_embed([first, second], days, title="Configuration Comparison")
            _set_command_footer(embed, "Advisory only", "No settings changed")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        @self.tree.command(
            name="resume-queue",
            description="Show tasks stopped by a usage limit",
        )
        async def resume_queue(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await interaction.response.send_message(
                embed=resume_queue_embed(self.storage.pending_resume_targets()),
                ephemeral=True,
            )

        async def resume_position(
            interaction: discord.Interaction, position: int
        ) -> None:
            sample = self.storage.latest_sample()
            if sample is not None and (
                sample.primary_exhausted
                or sample.secondary_exhausted
                or sample.ordinary_usage_allowed is False
            ):
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Resume unavailable",
                        "The usage limit is still active. No task was resumed.",
                        severity="warning",
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            success, detail, target = await asyncio.to_thread(
                self.service.resume_pending, position
            )
            label = target.label if target is not None else f"position {position}"
            if success:
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Task resumed",
                        f"Sent “{self.config.codex.resume_message}” to **{label}**.",
                        severity="success",
                    ),
                )
            else:
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Resume failed",
                        f"**{label}**\n`{detail[:500]}`",
                        severity="error",
                    ),
                )

        @self.tree.command(
            name="resume-next",
            description="Explicitly resume the next queued task",
        )
        async def resume_next(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await resume_position(interaction, 1)

        @self.tree.command(
            name="resume-select",
            description="Explicitly resume a task by its queue position",
        )
        @app_commands.describe(position="Position from /resume-queue, starting at 1")
        async def resume_select(
            interaction: discord.Interaction, position: int
        ) -> None:
            if not await self._authorized(interaction):
                return
            await resume_position(interaction, position)

        @self.tree.command(
            name="digest",
            description="Preview the current weekly digest",
        )
        async def digest(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await interaction.response.send_message(
                embed=weekly_digest_embed(self.storage, int(time.time())),
                ephemeral=True,
            )

        @self.tree.command(
            name="test-notification",
            description="Preview every alert type after 10 seconds",
        )
        async def test_notification(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            mention_id = (
                self.config.discord.mention_user_id
                or self.config.discord.owner_user_id
            )
            preview_count = len(
                notification_preview_alerts(self.config.codex.resume_message)
            )
            await interaction.response.send_message(
                embed=command_result_embed(
                    "Notification preview scheduled",
                    f"All {preview_count} alert previews will be sent in **{TEST_NOTIFICATION_DELAY_SECONDS} seconds**.",
                    severity="success",
                ),
                ephemeral=True,
            )
            task = asyncio.create_task(
                self._send_delayed_test_notification(mention_id),
                name="codex-pulse-test-notification",
            )
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)

        @self.tree.command(name="health", description="Show monitor health")
        async def health(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            success_at = self.storage.get_state("last_success_at")
            wake_at = self.storage.get_state("last_wake_at")
            last_error = self.last_error or self.storage.get_state("last_error")
            resume_error = self.storage.get_state("last_resume_error")
            analytics_error = self.storage.get_state("last_analytics_error")
            analytics_at = self.storage.get_state("last_session_import_at")
            maintenance_at = self.storage.get_state("last_storage_maintenance_at")
            storage_error = self.storage.get_state("last_storage_maintenance_error")
            queue_count = len(self.storage.pending_resume_targets())
            log_path = self.config.storage.log_path
            try:
                log_bytes = log_path.stat().st_size if log_path is not None else 0
            except FileNotFoundError:
                log_bytes = 0
            await interaction.response.send_message(
                embed=health_embed(
                    success_at=success_at,
                    wake_at=wake_at,
                    analytics_at=analytics_at,
                    interval_seconds=self.config.monitor.poll_seconds,
                    queue_count=queue_count,
                    auto_resume=self.config.codex.auto_resume,
                    last_error=last_error,
                    resume_error=resume_error,
                    analytics_error=analytics_error,
                    timezone=self.timezone,
                    database_bytes=self.storage.database_size_bytes(),
                    log_bytes=log_bytes,
                    retention_days=self.config.monitor.history_days,
                    maintenance_at=maintenance_at,
                    storage_error=storage_error,
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="computer",
            description="Show live computer resource and battery status",
        )
        async def computer(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                metrics = await asyncio.to_thread(collect_computer_metrics)
                await interaction.edit_original_response(
                    embed=computer_embed(metrics, self.timezone)
                )
            except Exception as error:
                LOGGER.exception("Computer metrics collection failed")
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Computer unavailable",
                        str(error)[:500],
                        severity="error",
                    )
                )

        @self.tree.command(
            name="screenshot",
            description="Privately capture every active display",
        )
        async def screenshot(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction):
                return
            if not interaction.app_permissions.attach_files:
                await interaction.response.send_message(
                    embed=command_result_embed(
                        "Screenshot permission required",
                        "Grant CodexPulse the **Attach Files** permission in this "
                        "channel, then try again.",
                        severity="error",
                    ),
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True, thinking=True)
            files: list[discord.File] = []
            try:
                with tempfile.TemporaryDirectory(
                    prefix="codex-pulse-screenshot-"
                ) as directory:
                    paths = await asyncio.to_thread(
                        capture_screenshots, Path(directory)
                    )
                    fitted = [
                        await asyncio.to_thread(
                            fit_screenshot_for_upload,
                            path,
                            int(interaction.filesize_limit),
                        )
                        for path in paths
                    ]
                    names = [
                        f"codex-pulse-display-{index}.jpg"
                        for index in range(1, len(fitted) + 1)
                    ]
                    files = [
                        discord.File(str(path), filename=name)
                        for path, name in zip(fitted, names)
                    ]
                    embeds = [
                        screenshot_embed(
                            name,
                            index,
                            len(names),
                            self.timezone,
                            self._command_response_delete_seconds(),
                        )
                        for index, name in enumerate(names, start=1)
                    ]
                    await interaction.edit_original_response(
                        embeds=embeds,
                        attachments=files,
                    )
            except ScreenCaptureError as error:
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Screenshot unavailable",
                        str(error)[:1000],
                        severity="error",
                    ),
                    attachments=[],
                )
            except discord.Forbidden:
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Screenshot upload blocked",
                        "Grant CodexPulse the **Attach Files** permission in this "
                        "channel, then try again.",
                        severity="error",
                    ),
                    attachments=[],
                )
            except Exception as error:
                LOGGER.exception("Screenshot command failed")
                await interaction.edit_original_response(
                    embed=command_result_embed(
                        "Screenshot unavailable",
                        str(error)[:1000],
                        severity="error",
                    ),
                    attachments=[],
                )
            finally:
                for file in files:
                    file.close()


def run_bot(config: AppConfig, storage: Storage, service: MonitorService) -> None:
    config.validate_for_discord()
    token = config.discord_token()
    bot = MonitorBot(config, storage, service)
    bot.run(token, log_handler=None)
