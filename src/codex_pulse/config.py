from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DEFAULT_CODEX_BINARY = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/"
    "CodexCLI.app/Contents/MacOS/codex"
)
DEFAULT_RESUME_MESSAGE = (
    "Resume the interrupted task from its last progress. Review completed and "
    "remaining work, avoid repeating completed steps, and continue the remaining "
    "implementation and validation."
)
LEGACY_RESUME_MESSAGE = "続きから再開"


@dataclass(frozen=True)
class CodexConfig:
    binary: str = DEFAULT_CODEX_BINARY
    thread: str = ""
    auto_detect_thread: bool = True
    auto_resume: bool = True
    resume_message: str = DEFAULT_RESUME_MESSAGE
    resume_scan_limit: int = 30
    resume_lookback_hours: int = 24
    app_server_timeout_seconds: int = 30
    resume_timeout_seconds: int = 60


@dataclass(frozen=True)
class DiscordConfig:
    application_id: int = 0
    guild_id: int = 0
    channel_id: int = 0
    owner_user_id: int = 0
    mention_user_id: int = 0
    token_env: str = "DISCORD_BOT_TOKEN"
    keychain_service: str = "CodexPulseDiscord"
    keychain_account: str = "bot-token"
    alert_delete_seconds: int = 90
    command_delete_seconds: int = 180
    pin_status_message: bool = False


@dataclass(frozen=True)
class MonitorConfig:
    poll_seconds: int = 300
    history_days: int = 90
    forecast_thresholds_minutes: Tuple[int, ...] = (30, 15, 5)
    anomaly_multiplier: float = 1.75
    anomaly_min_rate: float = 0.10
    minimum_rate: float = 0.01
    timezone: str = "Asia/Tokyo"
    cpu_alert_percent: float = 90.0
    cpu_recovery_percent: float = 75.0
    memory_alert_percent: float = 90.0
    memory_recovery_percent: float = 80.0
    battery_alert_percent: int = 15
    battery_recovery_percent: int = 25
    resource_alert_consecutive_samples: int = 2


@dataclass(frozen=True)
class AnalyticsConfig:
    enabled: bool = True
    sessions_dir: Path = field(
        default_factory=lambda: Path.home() / ".codex" / "sessions"
    )
    backfill_days: int = 14
    minimum_profile_samples: int = 3
    weekly_digest_enabled: bool = True
    weekly_digest_weekday: int = 0
    weekly_digest_hour: int = 9
    weekly_digest_mention: bool = False


@dataclass(frozen=True)
class StorageConfig:
    database: Path
    maintenance_interval_hours: int = 24
    resumed_queue_days: int = 30
    log_path: Optional[Path] = None
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 3


@dataclass(frozen=True)
class AppConfig:
    path: Path
    codex: CodexConfig
    discord: DiscordConfig
    monitor: MonitorConfig
    storage: StorageConfig
    analytics: AnalyticsConfig = field(default_factory=AnalyticsConfig)

    @property
    def root(self) -> Path:
        return self.path.parent

    def validate_for_discord(self) -> None:
        missing = []
        for name in ("application_id", "guild_id", "channel_id", "owner_user_id"):
            if not getattr(self.discord, name):
                missing.append(f"discord.{name}")
        if missing:
            raise ValueError("Missing Discord settings: " + ", ".join(missing))

    def discord_token(self) -> str:
        env_token = os.environ.get(self.discord.token_env, "").strip()
        if env_token:
            return env_token
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                self.discord.keychain_service,
                "-a",
                self.discord.keychain_account,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
        raise ValueError(
            f"Discord token not found in ${self.discord.token_env} or macOS Keychain "
            f"service {self.discord.keychain_service!r}"
        )


def _section(document: Dict[str, Any], name: str) -> Dict[str, Any]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


def _optional_float(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)


def load_config(path: Path) -> AppConfig:
    config_path = path.expanduser().absolute()
    with config_path.open("rb") as handle:
        document = tomllib.load(handle)

    codex = _section(document, "codex")
    discord = _section(document, "discord")
    monitor = _section(document, "monitor")
    analytics = _section(document, "analytics")
    storage = _section(document, "storage")

    database_value = str(storage.get("database", "data/usage.sqlite3"))
    database_path = Path(database_value).expanduser()
    if not database_path.is_absolute():
        database_path = config_path.parent / database_path
    log_path_value = str(
        storage.get("log_path", "~/Library/Logs/CodexPulse.log")
    ).strip()
    log_path = (
        Path(log_path_value).expanduser().absolute() if log_path_value else None
    )

    thresholds_value = monitor.get("forecast_thresholds_minutes", [30, 15, 5])
    if not isinstance(thresholds_value, list):
        raise ValueError("monitor.forecast_thresholds_minutes must be an array")
    thresholds = tuple(sorted({int(value) for value in thresholds_value}, reverse=True))
    resume_message = str(
        codex.get("resume_message", DEFAULT_RESUME_MESSAGE)
    ).strip()
    if not resume_message or resume_message == LEGACY_RESUME_MESSAGE:
        resume_message = DEFAULT_RESUME_MESSAGE

    return AppConfig(
        path=config_path,
        codex=CodexConfig(
            binary=str(codex.get("binary", DEFAULT_CODEX_BINARY)),
            thread=str(codex.get("thread", "")),
            auto_detect_thread=bool(codex.get("auto_detect_thread", True)),
            auto_resume=bool(codex.get("auto_resume", True)),
            resume_message=resume_message,
            resume_scan_limit=max(1, int(codex.get("resume_scan_limit", 30))),
            resume_lookback_hours=max(
                1, int(codex.get("resume_lookback_hours", 24))
            ),
            app_server_timeout_seconds=int(codex.get("app_server_timeout_seconds", 30)),
            resume_timeout_seconds=int(codex.get("resume_timeout_seconds", 60)),
        ),
        discord=DiscordConfig(
            application_id=int(discord.get("application_id", 0)),
            guild_id=int(discord.get("guild_id", 0)),
            channel_id=int(discord.get("channel_id", 0)),
            owner_user_id=int(discord.get("owner_user_id", 0)),
            mention_user_id=int(discord.get("mention_user_id", 0)),
            token_env=str(discord.get("token_env", "DISCORD_BOT_TOKEN")),
            keychain_service=str(
                discord.get("keychain_service", "CodexPulseDiscord")
            ),
            keychain_account=str(discord.get("keychain_account", "bot-token")),
            alert_delete_seconds=max(
                1, int(discord.get("alert_delete_seconds", 90))
            ),
            command_delete_seconds=max(
                1, int(discord.get("command_delete_seconds", 180))
            ),
            pin_status_message=bool(discord.get("pin_status_message", False)),
        ),
        monitor=MonitorConfig(
            poll_seconds=max(60, int(monitor.get("poll_seconds", 300))),
            history_days=max(1, int(monitor.get("history_days", 90))),
            forecast_thresholds_minutes=thresholds,
            anomaly_multiplier=_optional_float(
                monitor.get("anomaly_multiplier"), 1.75
            ),
            anomaly_min_rate=_optional_float(
                monitor.get("anomaly_min_rate"), 0.10
            ),
            minimum_rate=_optional_float(monitor.get("minimum_rate"), 0.01),
            timezone=str(monitor.get("timezone", "Asia/Tokyo")),
            cpu_alert_percent=max(
                1.0,
                min(100.0, _optional_float(monitor.get("cpu_alert_percent"), 90.0)),
            ),
            cpu_recovery_percent=max(
                0.0,
                min(
                    100.0,
                    _optional_float(monitor.get("cpu_recovery_percent"), 75.0),
                ),
            ),
            memory_alert_percent=max(
                1.0,
                min(
                    100.0,
                    _optional_float(monitor.get("memory_alert_percent"), 90.0),
                ),
            ),
            memory_recovery_percent=max(
                0.0,
                min(
                    100.0,
                    _optional_float(monitor.get("memory_recovery_percent"), 80.0),
                ),
            ),
            battery_alert_percent=max(
                1, min(100, int(monitor.get("battery_alert_percent", 15)))
            ),
            battery_recovery_percent=max(
                1, min(100, int(monitor.get("battery_recovery_percent", 25)))
            ),
            resource_alert_consecutive_samples=max(
                1, int(monitor.get("resource_alert_consecutive_samples", 2))
            ),
        ),
        storage=StorageConfig(
            database=database_path.absolute(),
            maintenance_interval_hours=max(
                1, int(storage.get("maintenance_interval_hours", 24))
            ),
            resumed_queue_days=max(
                1, int(storage.get("resumed_queue_days", 30))
            ),
            log_path=log_path,
            log_max_bytes=max(
                1024, int(storage.get("log_max_bytes", 5 * 1024 * 1024))
            ),
            log_backup_count=max(
                0, int(storage.get("log_backup_count", 3))
            ),
        ),
        analytics=AnalyticsConfig(
            enabled=bool(analytics.get("enabled", True)),
            sessions_dir=Path(
                str(analytics.get("sessions_dir", "~/.codex/sessions"))
            ).expanduser().absolute(),
            backfill_days=max(1, int(analytics.get("backfill_days", 14))),
            minimum_profile_samples=max(
                1, int(analytics.get("minimum_profile_samples", 3))
            ),
            weekly_digest_enabled=bool(
                analytics.get("weekly_digest_enabled", True)
            ),
            weekly_digest_weekday=max(
                0, min(6, int(analytics.get("weekly_digest_weekday", 0)))
            ),
            weekly_digest_hour=max(
                0, min(23, int(analytics.get("weekly_digest_hour", 9)))
            ),
            weekly_digest_mention=bool(
                analytics.get("weekly_digest_mention", False)
            ),
        ),
    )
