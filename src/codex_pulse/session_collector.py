from __future__ import annotations

import json
import logging
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import AnalyticsConfig
from .models import TurnUsage
from .storage import Storage


LOGGER = logging.getLogger(__name__)


def _int(value: Any) -> Optional[int]:
    return int(value) if isinstance(value, (int, float)) else None


def _timestamp(value: Any, fallback: int) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    return fallback


def _tokens(value: Any) -> dict[str, int]:
    source = value if isinstance(value, dict) else {}
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    return {
        name: max(0, int(source.get(name, 0)))
        if isinstance(source.get(name, 0), (int, float))
        else 0
        for name in names
    }


class SessionCollector:
    """Incrementally imports numeric Codex turn telemetry from rollout JSONL files."""

    def __init__(self, config: AnalyticsConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    def collect(self, observed_at: Optional[int] = None) -> int:
        if not self.config.enabled or not self.config.sessions_dir.is_dir():
            return 0
        now = int(time.time()) if observed_at is None else int(observed_at)
        cutoff = now - self.config.backfill_days * 86400
        imported = 0
        for path in self.config.sessions_dir.rglob("*.jsonl"):
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    continue
                imported += self._collect_file(path, stat.st_ino, stat.st_size, now)
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                LOGGER.warning("Could not import Codex session %s: %s", path, error)
        self.storage.set_state("last_session_import_at", now)
        self.storage.set_state("last_session_import_count", imported)
        return imported

    def _collect_file(self, path: Path, inode: int, size: int, now: int) -> int:
        path_text = str(path)
        offset = self.storage.get_session_offset(path_text, inode)
        if offset > size:
            offset = 0
        if offset == size:
            return 0

        meta_key = f"session_meta:{path_text}"
        metadata = self.storage.get_state(meta_key, {})
        if not isinstance(metadata, dict):
            metadata = {}
        turns: dict[str, TurnUsage] = {}
        imported = 0

        def current(turn_id: str) -> TurnUsage:
            existing = turns.get(turn_id) or self.storage.get_turn(turn_id)
            if existing is not None:
                turns[turn_id] = existing
                return existing
            cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
            thread_id = metadata.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id:
                thread_id = path.stem.rsplit("-", 5)[-1]
            value = TurnUsage(
                turn_id=turn_id,
                thread_id=thread_id,
                root_turn_id=None,
                session_file=path_text,
                started_at=None,
                completed_at=None,
                duration_ms=None,
                model="unknown",
                effort="unknown",
                cwd=cwd,
                project_name=Path(cwd).name if cwd else "Unknown",
                is_subagent=bool(metadata.get("is_subagent", False)),
                status="running",
                updated_at=now,
            )
            turns[turn_id] = value
            return value

        with path.open("r", encoding="utf-8") as handle:
            handle.seek(offset)
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                event_type = event.get("type")
                updated_at = _timestamp(event.get("timestamp"), now)

                if event_type == "session_meta":
                    thread_id = payload.get("session_id") or payload.get("id")
                    source = payload.get("source")
                    metadata = {
                        "thread_id": str(thread_id) if thread_id else "",
                        "cwd": payload.get("cwd"),
                        "is_subagent": bool(payload.get("parent_thread_id"))
                        or isinstance(source, dict),
                    }
                    self.storage.set_state(meta_key, metadata)
                    continue

                if event_type == "turn_context":
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str):
                        continue
                    value = current(turn_id)
                    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else value.cwd
                    turns[turn_id] = replace(
                        value,
                        root_turn_id=(
                            str(payload["root_turn_id"])
                            if payload.get("root_turn_id")
                            else value.root_turn_id
                        ),
                        model=str(payload.get("model") or value.model),
                        effort=str(payload.get("effort") or value.effort),
                        cwd=cwd,
                        project_name=Path(cwd).name if cwd else value.project_name,
                        updated_at=updated_at,
                    )
                    continue

                if event_type == "token_usage_record":
                    turn_id = payload.get("turn_id")
                    if not isinstance(turn_id, str):
                        continue
                    value = current(turn_id)
                    usage = _tokens(payload.get("turn_token_usage"))
                    request_usage = _tokens(payload.get("usage"))
                    turns[turn_id] = replace(
                        value,
                        thread_id=str(payload.get("thread_id") or value.thread_id),
                        root_turn_id=(
                            str(payload["root_turn_id"])
                            if payload.get("root_turn_id")
                            else value.root_turn_id
                        ),
                        input_tokens=usage["input_tokens"],
                        cached_input_tokens=usage["cached_input_tokens"],
                        cache_write_input_tokens=usage["cache_write_input_tokens"],
                        output_tokens=usage["output_tokens"],
                        reasoning_output_tokens=usage["reasoning_output_tokens"],
                        total_tokens=usage["total_tokens"],
                        max_request_input_tokens=max(
                            value.max_request_input_tokens,
                            request_usage["input_tokens"],
                        ),
                        updated_at=updated_at,
                    )
                    continue

                if event_type != "event_msg":
                    continue
                message_type = payload.get("type")
                turn_id = payload.get("turn_id")
                if not isinstance(turn_id, str):
                    continue
                value = current(turn_id)
                if message_type == "task_started":
                    turns[turn_id] = replace(
                        value,
                        root_turn_id=(
                            str(payload["root_turn_id"])
                            if payload.get("root_turn_id")
                            else value.root_turn_id
                        ),
                        started_at=_int(payload.get("started_at")) or value.started_at,
                        context_window=_int(payload.get("model_context_window"))
                        or value.context_window,
                        status="running",
                        updated_at=updated_at,
                    )
                elif message_type == "task_complete":
                    turns[turn_id] = replace(
                        value,
                        started_at=_int(payload.get("started_at")) or value.started_at,
                        completed_at=_int(payload.get("completed_at")) or updated_at,
                        duration_ms=_int(payload.get("duration_ms")),
                        status="completed",
                        updated_at=updated_at,
                    )

            new_offset = handle.tell()

        for turn in turns.values():
            self.storage.upsert_turn(turn)
            imported += 1
        self.storage.set_session_offset(path_text, inode, new_offset)
        return imported
