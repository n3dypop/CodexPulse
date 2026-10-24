from __future__ import annotations

import json
import selectors
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, Optional, Tuple

from .config import CodexConfig
from .models import ResumeTarget, UsageSample


class CodexError(RuntimeError):
    """Raised when Codex usage data cannot be collected."""


class CodexClient:
    def __init__(self, config: CodexConfig) -> None:
        self.config = config
        binary = Path(config.binary).expanduser()
        if not binary.is_file():
            raise CodexError(f"Codex CLI was not found: {binary}")
        self.binary = str(binary)

    @staticmethod
    def _send(process: subprocess.Popen[str], payload: Dict[str, Any]) -> None:
        if process.stdin is None:
            raise CodexError("Codex app-server stdin is unavailable")
        process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        process.stdin.flush()

    @staticmethod
    def _response(
        process: subprocess.Popen[str], request_id: int, deadline: float
    ) -> Dict[str, Any]:
        if process.stdout is None:
            raise CodexError("Codex app-server stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                if not selector.select(remaining):
                    break
                line = process.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise CodexError(
                        "Codex app-server returned an error: "
                        + json.dumps(message["error"], ensure_ascii=False)
                    )
                result = message.get("result")
                if not isinstance(result, dict):
                    raise CodexError("Codex app-server returned an invalid result")
                return result
        finally:
            selector.close()

        detail = ""
        if process.poll() is not None and process.stderr is not None:
            detail = process.stderr.read().strip()
        raise CodexError(
            "Timed out waiting for Codex app-server"
            + (f": {detail}" if detail else "")
        )

    @contextmanager
    def _app_server(
        self,
    ) -> Iterator[Callable[[str, Dict[str, Any]], Dict[str, Any]]]:
        try:
            process = subprocess.Popen(
                [self.binary, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as error:
            raise CodexError(str(error)) from error
        request_id = 0

        def request(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal request_id
            request_id += 1
            self._send(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                },
            )
            deadline = time.monotonic() + self.config.app_server_timeout_seconds
            return self._response(process, request_id, deadline)

        try:
            request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex-pulse",
                        "title": "CodexPulse",
                        "version": "3.11.0",
                    }
                },
            )
            self._send(
                process,
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            )
            yield request
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def read_rate_limits(self) -> Dict[str, Any]:
        with self._app_server() as request:
            return request(
                "account/rateLimits/read",
                {"excludeResetCreditDetails": False},
            )

    def collect(self, observed_at: Optional[int] = None) -> UsageSample:
        return parse_usage(self.read_rate_limits(), observed_at or int(time.time()))

    def find_resume_target(self, since: Optional[int] = None) -> Optional[ResumeTarget]:
        targets = self.find_resume_targets(since=since)
        return targets[0] if targets else None

    def find_resume_targets(self, since: Optional[int] = None) -> list[ResumeTarget]:
        cutoff = (
            int(since)
            if since is not None
            else int(time.time()) - self.config.resume_lookback_hours * 3600
        )
        matches: list[tuple[int, Dict[str, Any]]] = []
        project_names: dict[str, Optional[str]] = {}
        with self._app_server() as request:
            result = request(
                "thread/list",
                {
                    "limit": self.config.resume_scan_limit,
                    "sortKey": "updated_at",
                    "sortDirection": "desc",
                },
            )
            threads = result.get("data")
            if not isinstance(threads, list):
                raise CodexError("Codex thread/list returned invalid data")
            for thread in threads:
                if not isinstance(thread, dict):
                    continue
                thread_id = thread.get("id")
                if (
                    not isinstance(thread_id, str)
                    or thread.get("parentThreadId") is not None
                    or thread.get("ephemeral") is True
                ):
                    continue
                turns_result = request(
                    "thread/turns/list",
                    {
                        "threadId": thread_id,
                        "limit": 1,
                        "sortDirection": "desc",
                        "itemsView": "summary",
                    },
                )
                turns = turns_result.get("data")
                if not isinstance(turns, list) or not turns:
                    continue
                turn = turns[0]
                if not isinstance(turn, dict) or not _usage_limit_turn(turn):
                    continue
                stopped_at = _timestamp(turn.get("completedAt"))
                if stopped_at is None:
                    stopped_at = _timestamp(turn.get("startedAt"))
                if stopped_at is None:
                    stopped_at = _timestamp(thread.get("updatedAt"))
                if stopped_at is None or stopped_at < cutoff:
                    continue
                matches.append((stopped_at, thread))

            for _, thread in matches:
                project_id = thread.get("projectId")
                if not isinstance(project_id, str) or not project_id:
                    continue
                if project_id in project_names:
                    continue
                try:
                    project_result = request("project/read", {"projectId": project_id})
                    project = project_result.get("project")
                    project_names[project_id] = (
                        str(project["name"])
                        if isinstance(project, dict)
                        and isinstance(project.get("name"), str)
                        else None
                    )
                except CodexError:
                    project_names[project_id] = None

        targets: list[ResumeTarget] = []
        for stopped_at, thread in sorted(matches, key=lambda item: item[0], reverse=True):
            project_value = thread.get("projectId")
            project_id = (
                project_value
                if isinstance(project_value, str) and project_value
                else None
            )
            name = thread.get("name")
            targets.append(
                ResumeTarget(
                    thread_id=str(thread["id"]),
                    thread_name=name if isinstance(name, str) and name.strip() else None,
                    project_id=project_id,
                    project_name=project_names.get(project_id) if project_id else None,
                    stopped_at=stopped_at,
                )
            )
        return targets

    def resume_thread(self, target: Optional[ResumeTarget] = None) -> Tuple[bool, str]:
        thread_id = target.thread_id if target is not None else self.config.thread.strip()
        if not thread_id:
            return False, "codex.thread is not configured"
        command = [
            self.binary,
            "queue",
            "--thread",
            thread_id,
            "--message",
            self.config.resume_message,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.config.resume_timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, str(error)
        detail = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode == 0:
            return True, detail or "Resume message queued"
        return False, detail or f"codex queue exited with {completed.returncode}"


def _timestamp(value: Any) -> Optional[int]:
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _usage_limit_turn(turn: Dict[str, Any]) -> bool:
    if turn.get("status") not in {"failed", "interrupted"}:
        return False
    error = turn.get("error")
    if not isinstance(error, dict):
        return False
    error_info = error.get("codexErrorInfo")
    if error_info in {"usageLimitExceeded", "rateLimitExceeded"}:
        return True
    text = " ".join(
        str(error.get(field) or "") for field in ("message", "additionalDetails")
    ).lower()
    return (
        "usage limit" in text or "rate limit" in text
    ) and ("reached" in text or "exceeded" in text)


def _snapshots(result: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, Any]]]:
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict):
        for limit_id, snapshot in by_id.items():
            if isinstance(snapshot, dict):
                yield str(limit_id), snapshot
        return
    snapshot = result.get("rateLimits")
    if isinstance(snapshot, dict):
        yield str(snapshot.get("limitId") or "codex"), snapshot


def _window(value: Any) -> Optional[Tuple[float, int, int]]:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    minutes = value.get("windowDurationMins")
    resets_at = value.get("resetsAt")
    if not all(isinstance(item, (int, float)) for item in (used, minutes, resets_at)):
        return None
    return float(used), int(minutes), int(resets_at)


def _optional_bool(value: Any) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _credits(result: Dict[str, Any]) -> Tuple[Optional[float], bool, bool]:
    value = result.get("credits")
    if not isinstance(value, dict):
        return None, False, False
    balance = value.get("balance")
    if isinstance(balance, str):
        try:
            balance = float(balance)
        except ValueError:
            balance = None
    elif not isinstance(balance, (int, float)):
        balance = None
    return (
        float(balance) if balance is not None else None,
        bool(value.get("hasCredits", balance is not None)),
        bool(value.get("unlimited", False)),
    )


def parse_usage(result: Dict[str, Any], observed_at: int) -> UsageSample:
    snapshots = list(_snapshots(result))
    candidates = []
    for limit_id, snapshot in snapshots:
        for slot in ("primary", "secondary", "individualLimit"):
            window = _window(snapshot.get(slot))
            if window is not None:
                candidates.append((limit_id, slot, window, snapshot))
    recognized = bool(snapshots) or any(
        key in result
        for key in (
            "planType",
            "ordinaryUsageAllowed",
            "credits",
            "rateLimits",
            "rateLimitsByLimitId",
        )
    )
    if not candidates and not recognized:
        raise CodexError("Codex did not return a usable rate-limit window")

    five_hour_candidates = [
        item for item in candidates if abs(item[2][1] - 300) <= 15
    ]
    primary_entry = (
        min(
            five_hour_candidates,
            key=lambda item: (
                abs(item[2][1] - 300),
                item[0] != "codex",
                item[1] != "primary",
            ),
        )
        if five_hour_candidates
        else None
    )
    if primary_entry is not None:
        primary_used, primary_minutes, primary_reset = primary_entry[2]
    else:
        primary_used, primary_minutes, primary_reset = 0.0, 0, 0

    secondary_candidates = [
        item for item in candidates if item[2][1] > 315
    ]
    if secondary_candidates:
        secondary_entry = min(
            secondary_candidates,
            key=lambda item: (
                item[0] != (primary_entry[0] if primary_entry else item[0]),
                item[1] != "secondary",
                -item[2][1],
            ),
        )
        secondary_used, secondary_minutes, secondary_reset = secondary_entry[2]
    else:
        secondary_entry = None
        secondary_used = secondary_minutes = secondary_reset = None

    metadata_entry = primary_entry or secondary_entry
    snapshot = (
        metadata_entry[3]
        if metadata_entry is not None
        else snapshots[0][1]
        if snapshots
        else result
    )
    reached_type = snapshot.get("rateLimitReachedType")
    plan_type = snapshot.get("planType", result.get("planType"))
    model_slug = result.get("modelSlug")
    credit_source = snapshot if isinstance(snapshot.get("credits"), dict) else result
    balance, has_credits, unlimited = _credits(credit_source)
    return UsageSample(
        observed_at=int(observed_at),
        primary_used_percent=max(0.0, min(100.0, primary_used)),
        primary_window_minutes=primary_minutes,
        primary_resets_at=primary_reset,
        secondary_used_percent=(
            max(0.0, min(100.0, float(secondary_used)))
            if secondary_used is not None
            else None
        ),
        secondary_window_minutes=secondary_minutes,
        secondary_resets_at=secondary_reset,
        credits_balance=balance,
        credits_has_credits=has_credits,
        credits_unlimited=unlimited,
        ordinary_usage_allowed=_optional_bool(result.get("ordinaryUsageAllowed")),
        plan_type=str(plan_type) if plan_type is not None else None,
        limit_id=metadata_entry[0] if metadata_entry is not None else "codex",
        limit_reached_type=str(reached_type) if reached_type is not None else None,
        model_slug=str(model_slug) if model_slug is not None else None,
        primary_limit_available=primary_entry is not None,
    )
