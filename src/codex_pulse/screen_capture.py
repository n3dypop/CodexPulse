from __future__ import annotations

import subprocess
from pathlib import Path


SCREEN_CAPTURE_BINARY = "/usr/sbin/screencapture"
SIPS_BINARY = "/usr/bin/sips"
MAX_DISPLAYS_PER_MESSAGE = 10


class ScreenCaptureError(RuntimeError):
    pass


def capture_screenshots(output_directory: Path) -> list[Path]:
    """Capture every active display into temporary JPEG files."""
    target = output_directory / "codex-pulse-screen.jpg"
    try:
        completed = subprocess.run(
            [
                SCREEN_CAPTURE_BINARY,
                "-x",
                "-C",
                "-t",
                "jpg",
                str(target),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ScreenCaptureError(f"Screen capture failed: {error}") from error

    paths = sorted(
        (
            path
            for path in output_directory.glob("codex-pulse-screen*")
            if path.is_file() and path.stat().st_size > 0
        ),
        key=lambda path: (path != target, path.name),
    )
    if completed.returncode != 0 or not paths:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if "could not create image from display" in detail.casefold():
            detail = (
                "macOS blocked screen capture. Open System Settings → Privacy & "
                "Security → Screen & System Audio Recording, enable CodexPulse "
                "(it may appear as Python or screencapture), then restart the bot."
            )
        raise ScreenCaptureError(
            detail
            or "macOS did not return an image. Allow Screen & System Audio "
            "Recording for CodexPulse, then restart the bot."
        )
    if len(paths) > MAX_DISPLAYS_PER_MESSAGE:
        raise ScreenCaptureError(
            f"{len(paths)} displays were captured, but Discord accepts at most "
            f"{MAX_DISPLAYS_PER_MESSAGE} attachments in one response."
        )
    return paths


def fit_screenshot_for_upload(path: Path, maximum_bytes: int) -> Path:
    """Re-encode or resize a screenshot only when Discord's limit requires it."""
    if path.stat().st_size <= maximum_bytes:
        return path

    candidate = path.with_name(f"{path.stem}-upload.jpg")
    for quality in (70, 45, 25):
        completed = subprocess.run(
            [
                SIPS_BINARY,
                "-s",
                "format",
                "jpeg",
                "-s",
                "formatOptions",
                str(quality),
                str(path),
                "--out",
                str(candidate),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if (
            completed.returncode == 0
            and candidate.exists()
            and candidate.stat().st_size <= maximum_bytes
        ):
            return candidate

    completed = subprocess.run(
        [
            SIPS_BINARY,
            "-Z",
            "2560",
            "-s",
            "format",
            "jpeg",
            "-s",
            "formatOptions",
            "60",
            str(path),
            "--out",
            str(candidate),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if (
        completed.returncode == 0
        and candidate.exists()
        and candidate.stat().st_size <= maximum_bytes
    ):
        return candidate
    raise ScreenCaptureError(
        f"Screenshot is larger than Discord's {maximum_bytes} byte upload limit."
    )
