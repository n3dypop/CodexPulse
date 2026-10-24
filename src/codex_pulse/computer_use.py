from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ComputerUseStatus:
    state: str
    last_used_at: Optional[int] = None


def _timestamp(value: Any, fallback: int) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return fallback


def _is_computer_use(payload: dict[str, Any]) -> bool:
    namespace = str(payload.get("namespace") or "").casefold()
    name = str(payload.get("name") or "").casefold()
    return namespace in {"mcp__cua_repl", "computer", "computer_use"} or name in {
        "computer_use",
        "cua_repl",
    }


def _tail_events(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("rb") as handle:
        size = handle.seek(0, 2)
        start = max(0, size - max_bytes)
        handle.seek(start)
        if start:
            handle.readline()
        for raw_line in handle:
            try:
                event = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                events.append(event)
    return events


def detect_computer_use(
    sessions_dir: Path,
    observed_at: Optional[int] = None,
    *,
    active_timeout_seconds: int = 600,
    current_turn_timeout_seconds: int = 1800,
    candidate_hours: int = 24,
    max_candidates: int = 20,
    max_bytes_per_file: int = 2_000_000,
) -> ComputerUseStatus:
    now = int(time.time()) if observed_at is None else int(observed_at)
    if not sessions_dir.is_dir():
        return ComputerUseStatus("unavailable")

    candidates: list[tuple[float, Path]] = []
    cutoff = now - max(1, candidate_hours) * 3600
    try:
        for path in sessions_dir.rglob("*.jsonl"):
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                continue
            if modified_at >= cutoff:
                candidates.append((modified_at, path))
    except OSError:
        return ComputerUseStatus("unavailable")
    candidates.sort(reverse=True, key=lambda item: item[0])

    active_calls: dict[str, int] = {}
    used_in_open_turn = False
    newest_open_turn_activity = 0
    last_used_at: Optional[int] = None

    for modified_at, path in candidates[: max(1, max_candidates)]:
        open_turn = False
        used_in_file_turn = False
        file_active_calls: dict[str, int] = {}
        try:
            events = _tail_events(path, max_bytes_per_file)
        except OSError:
            continue
        for event in events:
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            timestamp = _timestamp(event.get("timestamp"), int(modified_at))
            if event.get("type") == "event_msg":
                message_type = payload.get("type")
                if message_type == "task_started":
                    open_turn = True
                    used_in_file_turn = False
                    file_active_calls.clear()
                elif message_type == "task_complete":
                    open_turn = False
                    used_in_file_turn = False
                    file_active_calls.clear()
                continue
            if event.get("type") != "response_item":
                continue
            item_type = payload.get("type")
            call_id = payload.get("call_id")
            if item_type in {"function_call", "custom_tool_call"} and _is_computer_use(
                payload
            ):
                open_turn = True
                used_in_file_turn = True
                last_used_at = max(last_used_at or 0, timestamp)
                if isinstance(call_id, str):
                    file_active_calls[call_id] = timestamp
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                if isinstance(call_id, str):
                    file_active_calls.pop(call_id, None)

        if open_turn and used_in_file_turn:
            newest_open_turn_activity = max(newest_open_turn_activity, int(modified_at))
            if now - modified_at <= current_turn_timeout_seconds:
                used_in_open_turn = True
        active_calls.update(file_active_calls)

    if any(now - timestamp <= active_timeout_seconds for timestamp in active_calls.values()):
        return ComputerUseStatus("active", last_used_at)
    if used_in_open_turn and now - newest_open_turn_activity <= current_turn_timeout_seconds:
        return ComputerUseStatus("used_this_turn", last_used_at)
    return ComputerUseStatus("idle", last_used_at)
