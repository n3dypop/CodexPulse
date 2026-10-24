from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .codex_client import CodexClient
from .analytics import rebuild_attributions
from .config import AppConfig
from .forecast import forecast_usage
from .models import Alert, CycleResult, ResumeTarget, UsageSample
from .storage import Storage
from .session_collector import SessionCollector
from .time_format import approximate_duration


LOGGER = logging.getLogger(__name__)


def _progress_bar(percent: float, width: int = 16) -> str:
    clamped = max(0.0, min(100.0, percent))
    filled = round(clamped / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _limit_alert_value(percent: float, detail: str) -> str:
    return f"`{_progress_bar(percent)}`  **{percent:.0f}%**\n{detail}"


def _alert_sections(headline: str, detail: str) -> str:
    return f"{headline}\n\u200b\n{detail}"


def _file_size(path: Optional[Path]) -> int:
    if path is None:
        return 0
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _rotate_log(path: Path, max_bytes: int, backup_count: int) -> bool:
    try:
        if path.stat().st_size <= max(1, int(max_bytes)):
            return False
    except FileNotFoundError:
        return False

    copies = max(0, int(backup_count))
    if copies:
        oldest = path.with_name(f"{path.name}.{copies}")
        oldest.unlink(missing_ok=True)
        for index in range(copies - 1, 0, -1):
            source = path.with_name(f"{path.name}.{index}")
            if source.exists():
                source.replace(path.with_name(f"{path.name}.{index + 1}"))
        first = path.with_name(f"{path.name}.1")
        shutil.copyfile(path, first)
        first.chmod(0o600)

    with path.open("w", encoding="utf-8"):
        pass
    path.chmod(0o600)
    return True


class MonitorService:
    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        client: Optional[CodexClient] = None,
        session_collector: Optional[SessionCollector] = None,
    ) -> None:
        self.config = config
        self.storage = storage
        self.client = client or CodexClient(config.codex)
        self.session_collector = session_collector
        if session_collector is None and isinstance(self.client, CodexClient):
            self.session_collector = SessionCollector(config.analytics, storage)

    @staticmethod
    def _reset_detected(
        previous: Optional[UsageSample], current: UsageSample
    ) -> bool:
        if previous is None or not previous.primary_exhausted:
            return False
        rolled_forward = current.primary_resets_at > previous.primary_resets_at + 60
        became_available = (
            current.observed_at >= previous.primary_resets_at - 120
            and not current.primary_exhausted
        )
        return rolled_forward or became_available

    @staticmethod
    def _window_rolled_forward(
        previous: Optional[UsageSample], current: UsageSample
    ) -> bool:
        return (
            previous is not None
            and previous.primary_limit_available
            and current.primary_limit_available
            and current.primary_resets_at > previous.primary_resets_at + 60
            and not current.primary_exhausted
        )

    @staticmethod
    def _resume_target(value: Any) -> Optional[ResumeTarget]:
        if not isinstance(value, dict):
            return None
        try:
            return ResumeTarget(
                thread_id=str(value["thread_id"]),
                thread_name=(
                    str(value["thread_name"])
                    if value.get("thread_name") is not None
                    else None
                ),
                project_id=(
                    str(value["project_id"])
                    if value.get("project_id") is not None
                    else None
                ),
                project_name=(
                    str(value["project_name"])
                    if value.get("project_name") is not None
                    else None
                ),
                stopped_at=int(value["stopped_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _discover_resume_target(self, since: int) -> Optional[ResumeTarget]:
        if not self.config.codex.auto_detect_thread:
            return None
        try:
            finder = getattr(self.client, "find_resume_targets", None)
            if callable(finder):
                targets = finder(since=since)
            else:
                target = self.client.find_resume_target(since=since)
                targets = [target] if target is not None else []
        except Exception as error:
            detail = f"Could not inspect Codex tasks: {error}"
            self.storage.set_state("last_resume_error", detail)
            LOGGER.warning(detail)
            return None
        if targets:
            self.storage.enqueue_resume_targets(targets)
            target = targets[0]
            self.storage.set_state("pending_resume_target", target.as_dict())
            self.storage.set_state("last_resume_error", None)
            return target
        return None

    def resume_pending(self, position: int = 1) -> tuple[bool, str, Optional[ResumeTarget]]:
        targets = self.storage.pending_resume_targets()
        if position < 1 or position > len(targets):
            return False, "No pending task at that position", None
        target = targets[position - 1]
        success, detail = self.client.resume_thread(target)
        if success:
            self.storage.mark_resume_target_resumed(target.thread_id)
        return success, detail, target

    def _maybe_maintain(self, now: int) -> None:
        interval = self.config.storage.maintenance_interval_hours * 3600
        last_run = self.storage.get_state("last_storage_maintenance_at", 0)
        if isinstance(last_run, (int, float)):
            elapsed = now - int(last_run)
            if 0 <= elapsed < interval:
                return

        history_before = now - self.config.monitor.history_days * 86400
        resumed_before = now - self.config.storage.resumed_queue_days * 86400
        try:
            report = self.storage.maintain(history_before, resumed_before)
        except Exception as error:
            LOGGER.warning("Storage maintenance failed: %s", error)
            self.storage.set_state("last_storage_maintenance_error", str(error))
            return

        rotated = False
        log_error: Optional[str] = None
        log_path = self.config.storage.log_path
        if log_path is not None:
            try:
                rotated = _rotate_log(
                    log_path,
                    self.config.storage.log_max_bytes,
                    self.config.storage.log_backup_count,
                )
            except OSError as error:
                log_error = str(error)
                LOGGER.warning("Log rotation failed: %s", error)

        report["log_rotated"] = rotated
        report["log_bytes"] = _file_size(log_path)
        self.storage.set_state("last_storage_maintenance_at", now)
        self.storage.set_state("last_storage_maintenance_report", report)
        self.storage.set_state("last_storage_maintenance_error", log_error)

    def run_cycle(self, observed_at: Optional[int] = None) -> CycleResult:
        now = int(time.time()) if observed_at is None else int(observed_at)
        previous = self.storage.latest_sample()
        current = self.client.collect(now)
        self.storage.add_sample(current)
        self._maybe_maintain(now)
        if self.session_collector is not None:
            try:
                self.session_collector.collect(now)
                rebuild_attributions(
                    self.storage,
                    now - self.config.analytics.backfill_days * 86400,
                )
                self.storage.set_state("last_analytics_error", None)
            except Exception as error:
                LOGGER.warning("Turn analytics refresh failed: %s", error)
                self.storage.set_state("last_analytics_error", str(error))
        history = self.storage.recent_samples(now - 7 * 86400)
        forecast = forecast_usage(history, self.config.monitor)
        alerts: list[Alert] = []

        exhaustion_started = current.primary_exhausted and (
            previous is None
            or not previous.primary_exhausted
            or previous.primary_resets_at != current.primary_resets_at
        )
        if exhaustion_started:
            self.storage.resolve_predictions(
                current.primary_resets_at,
                actual_exhaustion_at=now,
                outcome="exhausted",
            )
            key = f"exhausted:{current.primary_resets_at}"
            if not self.storage.has_event(key):
                alerts.append(
                    Alert(
                        key,
                        "5-hour limit reached",
                        _limit_alert_value(0, "Waiting for the 5-hour reset."),
                        "critical",
                    )
                )
            if self.config.codex.auto_resume and self.config.codex.auto_detect_thread:
                earliest = now - self.config.codex.resume_lookback_hours * 3600
                if previous is not None:
                    earliest = max(
                        earliest,
                        previous.observed_at - self.config.monitor.poll_seconds * 2,
                    )
                self._discover_resume_target(earliest)

        weekly_exhaustion_started = current.secondary_exhausted and (
            previous is None
            or not previous.secondary_exhausted
            or previous.secondary_resets_at != current.secondary_resets_at
        )
        if weekly_exhaustion_started and current.secondary_resets_at is not None:
            key = f"exhausted:weekly:{current.secondary_resets_at}"
            if not self.storage.has_event(key):
                alerts.append(
                    Alert(
                        key,
                        "Weekly limit reached",
                        _limit_alert_value(0, "Waiting for the weekly reset."),
                        "critical",
                    )
                )

        reset_detected = self._reset_detected(previous, current)
        if (
            previous is not None
            and self._window_rolled_forward(previous, current)
            and not previous.primary_exhausted
        ):
            self.storage.resolve_predictions(
                previous.primary_resets_at,
                actual_exhaustion_at=None,
                outcome="reset_first",
            )
        resume_target = self._resume_target(
            self.storage.get_state("pending_resume_target")
        )
        if (
            not reset_detected
            and self.config.codex.auto_resume
            and self.config.codex.auto_detect_thread
            and self._window_rolled_forward(previous, current)
            and previous is not None
        ):
            resume_target = self._discover_resume_target(
                previous.observed_at - self.config.monitor.poll_seconds * 2
            )
            reset_detected = resume_target is not None

        if reset_detected and previous is not None:
            if self.config.codex.auto_detect_thread and resume_target is None:
                resume_target = self._discover_resume_target(
                    now - self.config.codex.resume_lookback_hours * 3600
                )
            reset_key = f"reset:{previous.primary_resets_at}"
            if not self.storage.has_event(reset_key):
                alerts.append(
                    Alert(
                        reset_key,
                        "5-hour limit reset",
                        _limit_alert_value(
                            current.primary_remaining_percent,
                            "5-hour usage is available again.",
                        ),
                        "success",
                    )
                )
            self.storage.set_state("pending_resume_reset_at", previous.primary_resets_at)

        forecast_window = forecast.fast_limiting_window
        if forecast_window == "weekly":
            forecast_reset = current.secondary_resets_at
            forecast_remaining = current.secondary_remaining_percent
            forecast_exhausted = current.secondary_exhausted
            forecast_label = "weekly"
            forecast_key_prefix = "forecast:weekly"
        else:
            forecast_reset = current.primary_resets_at
            forecast_remaining = current.primary_remaining_percent
            forecast_exhausted = current.primary_exhausted
            forecast_label = "5-hour"
            forecast_key_prefix = "forecast"
        if (
            forecast.fast_minutes_to_exhaustion is not None
            and forecast_window is not None
            and forecast_reset is not None
            and forecast_remaining is not None
            and not forecast.reset_before_exhaustion
            and not forecast_exhausted
        ):
            eligible = [
                threshold
                for threshold in self.config.monitor.forecast_thresholds_minutes
                if forecast.fast_minutes_to_exhaustion <= threshold
                and not self.storage.has_event(
                    f"{forecast_key_prefix}:{forecast_reset}:{threshold}"
                )
            ]
            if eligible:
                threshold = min(eligible)
                key = f"{forecast_key_prefix}:{forecast_reset}:{threshold}"
                alerts.append(
                    Alert(
                        key,
                        f"{forecast_label.capitalize()} limit forecast",
                        _alert_sections(
                            f"## {approximate_duration(forecast.fast_minutes_to_exhaustion).removeprefix('~')}\n"
                            f"Estimated time to reach the {forecast_label} limit.",
                            f"`{_progress_bar(forecast_remaining)}`  "
                            f"**{forecast_remaining:.0f}%**",
                        ),
                        related_event_keys=tuple(
                            f"{forecast_key_prefix}:{forecast_reset}:{value}"
                            for value in eligible
                        ),
                    )
                )

        if (
            forecast.estimated_exhaustion_at is not None
            and not forecast.reset_before_exhaustion
            and forecast.limiting_window == "5-hour"
            and not current.primary_exhausted
        ):
            self.storage.add_prediction(
                now,
                current.primary_resets_at,
                forecast.estimated_exhaustion_at,
                current.primary_remaining_percent,
                forecast.confidence,
            )

        if forecast.anomalous_consumption:
            anomaly_window = forecast.limiting_window or "5-hour"
            anomaly_reset = (
                current.secondary_resets_at
                if anomaly_window == "weekly"
                else current.primary_resets_at
            )
            key = f"anomaly:{anomaly_window}:{anomaly_reset}"
            if not self.storage.has_event(key):
                alerts.append(
                    Alert(
                        key,
                        "Usage rate increased",
                        (
                            "```\n"
                            f"{'15 min':<12}{forecast.rate_15m:.2f}%/min\n"
                            f"{'60 min':<12}{forecast.rate_60m:.2f}%/min\n"
                            "```"
                        ),
                    )
                )

        resumed = False
        resume_detail: Optional[str] = None
        pending_reset = self.storage.get_state("pending_resume_reset_at")
        if (
            pending_reset is not None
            and self.config.codex.auto_resume
            and current.ordinary_usage_allowed is not False
        ):
            resume_event = f"resumed:{pending_reset}"
            if not self.storage.has_event(resume_event):
                resume_target = (
                    self._resume_target(
                        self.storage.get_state("pending_resume_target")
                    )
                    if self.config.codex.auto_detect_thread
                    else None
                )
                if self.config.codex.auto_detect_thread and resume_target is None:
                    resume_target = self._discover_resume_target(
                        now - self.config.codex.resume_lookback_hours * 3600
                    )
                if self.config.codex.auto_detect_thread and resume_target is None:
                    resumed = False
                    resume_detail = "No recent task stopped by a Codex usage limit was found"
                else:
                    resumed, resume_detail = self.client.resume_thread(resume_target)
                if resumed:
                    self.storage.mark_event(
                        resume_event, {"detail": resume_detail, "at": now}
                    )
                    self.storage.set_state("pending_resume_reset_at", None)
                    self.storage.set_state("pending_resume_target", None)
                    self.storage.set_state("last_resume_error", None)
                    if resume_target is not None:
                        self.storage.mark_resume_target_resumed(resume_target.thread_id)
                    key = f"resume-notice:{pending_reset}"
                    if not self.storage.has_event(key):
                        reset_key = f"reset:{pending_reset}"
                        alerts = [
                            alert for alert in alerts if alert.event_key != reset_key
                        ]
                        destination = "the selected task"
                        if resume_target is not None:
                            destination = f"**{resume_target.label[:120]}**"
                            if resume_target.project_name:
                                destination += f" · **{resume_target.project_name[:120]}**"
                        alerts.append(
                            Alert(
                                key,
                                "Limit reset · Work resumed",
                                _limit_alert_value(
                                    current.primary_remaining_percent,
                                    f"Resume prompt sent to {destination}.",
                                ),
                                "success",
                                (reset_key,),
                            )
                        )
                else:
                    self.storage.set_state("last_resume_error", resume_detail)
                    key = f"resume-failed:{pending_reset}"
                    if not self.storage.has_event(key):
                        alerts.append(
                            Alert(
                                key,
                                "Automatic resume failed",
                                "Retry scheduled for the next refresh.\n"
                                "Use `/health` for diagnostics.",
                                "critical",
                            )
                        )

        return CycleResult(
            sample=current,
            forecast=forecast,
            alerts=tuple(alerts),
            resumed=resumed,
            resume_detail=resume_detail,
        )
