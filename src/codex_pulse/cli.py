from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence

from .codex_client import CodexClient
from .config import AppConfig, load_config
from .forecast import forecast_usage
from .service import MonitorService
from .storage import Storage


APP_NAME = "CodexPulse"
LAUNCHD_LABEL = "com.local.codex-pulse"


def application_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / APP_NAME


def default_config_path() -> Path:
    return application_support_dir() / "config.toml"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def logs_dir() -> Path:
    return Path.home() / "Library" / "Logs"


def _configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _load(path: str) -> tuple[AppConfig, Storage, MonitorService]:
    config = load_config(Path(path))
    storage = Storage(config.storage.database)
    service = MonitorService(config, storage)
    return config, storage, service


def _security(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/security", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _set_token(config: AppConfig) -> int:
    token = getpass.getpass("Discord Bot Token: ").strip()
    if not token:
        print("Token was empty; no changes were made.", file=sys.stderr)
        return 2
    result = _security(
        [
            "add-generic-password",
            "-U",
            "-s",
            config.discord.keychain_service,
            "-a",
            config.discord.keychain_account,
            "-w",
            token,
        ]
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "Could not save token", file=sys.stderr)
        return result.returncode
    print(f"Discord token saved in macOS Keychain ({config.discord.keychain_service}).")
    return 0


def _delete_token(config: AppConfig) -> int:
    result = _security(
        [
            "delete-generic-password",
            "-s",
            config.discord.keychain_service,
            "-a",
            config.discord.keychain_account,
        ]
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "Token was not found", file=sys.stderr)
        return result.returncode
    print("Discord token removed from macOS Keychain.")
    return 0


def _invite_url(config: AppConfig) -> str:
    config.validate_for_discord()
    permissions = 117760  # View, Send, Embed, Attach Files, Read History
    if config.discord.pin_status_message:
        permissions |= 1 << 51  # PIN_MESSAGES
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={config.discord.application_id}"
        f"&permissions={permissions}&scope=bot%20applications.commands"
    )


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


def _install_launchd(config: AppConfig) -> int:
    config.validate_for_discord()
    config.discord_token()
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    logs_dir().mkdir(parents=True, exist_ok=True)
    log_path = logs_dir() / f"{APP_NAME}.log"
    value = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            sys.executable,
            "-m",
            "codex_pulse",
            "--config",
            str(config.path),
            "run",
        ],
        "WorkingDirectory": str(config.root),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    temporary = target.with_suffix(".plist.tmp")
    with temporary.open("wb") as handle:
        plistlib.dump(value, handle, sort_keys=True)
    temporary.chmod(0o644)
    temporary.replace(target)

    subprocess.run(
        ["/bin/launchctl", "bootout", _launchctl_domain(), str(target)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    result = subprocess.run(
        ["/bin/launchctl", "bootstrap", _launchctl_domain(), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(result.stderr.strip() or "launchctl bootstrap failed", file=sys.stderr)
        return result.returncode
    print(f"Installed and started {LAUNCHD_LABEL}.")
    print(f"Log: {log_path}")
    return 0


def _uninstall_launchd() -> int:
    target = plist_path()
    if target.exists():
        subprocess.run(
            ["/bin/launchctl", "bootout", _launchctl_domain(), str(target)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        target.unlink()
    print(f"Stopped {LAUNCHD_LABEL}; usage history and Keychain token were kept.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-pulse",
        description="Discord bot for Codex usage monitoring and forecasting",
    )
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Run the Discord bot")
    commands.add_parser("collect-once", help="Collect and print one usage sample")
    commands.add_parser("set-token", help="Save the Discord bot token to Keychain")
    commands.add_parser("delete-token", help="Delete the Discord bot token from Keychain")
    commands.add_parser("invite-url", help="Print the Discord bot invitation URL")
    commands.add_parser("install-launchd", help="Install and start the LaunchAgent")
    commands.add_parser("uninstall-launchd", help="Stop and remove the LaunchAgent")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    _configure_logging(arguments.verbose)
    try:
        config, storage, service = _load(arguments.config)
        if arguments.command == "run":
            from .discord_bot import run_bot

            run_bot(config, storage, service)
            return 0
        if arguments.command == "collect-once":
            result = service.run_cycle()
            print(
                json.dumps(
                    {
                        "sample": result.sample.as_dict(),
                        "forecast": result.forecast.as_dict(),
                        "alerts": [alert.__dict__ for alert in result.alerts],
                        "resumed": result.resumed,
                        "resume_detail": result.resume_detail,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if arguments.command == "set-token":
            return _set_token(config)
        if arguments.command == "delete-token":
            return _delete_token(config)
        if arguments.command == "invite-url":
            print(_invite_url(config))
            return 0
        if arguments.command == "install-launchd":
            return _install_launchd(config)
        if arguments.command == "uninstall-launchd":
            return _uninstall_launchd()
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
