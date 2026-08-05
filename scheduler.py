"""Configuration and macOS launchd helpers for scheduled JobHunter searches."""
from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


INCREMENTAL_LABEL = "au.jobhunter.incremental"
FULL_LABEL = "au.jobhunter.full"
MIN_INCREMENTAL_INTERVAL_MINUTES = 30
MAX_INCREMENTAL_INTERVAL_MINUTES = 24 * 60

DEFAULT_SCHEDULED_SEARCH: dict[str, Any] = {
    "incremental_enabled": True,
    "incremental_interval_minutes": 180,
    "full_enabled": True,
    "full_time": "02:00",
}

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _as_interval(value: Any) -> int:
    try:
        interval = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("增量搜索间隔必须是整数分钟") from exc
    if not MIN_INCREMENTAL_INTERVAL_MINUTES <= interval <= MAX_INCREMENTAL_INTERVAL_MINUTES:
        raise ValueError(
            "增量搜索间隔必须在 30 到 1440 分钟之间，避免请求过于频繁"
        )
    return interval


def _as_time(value: Any) -> str:
    full_time = str(value or "").strip()
    if not _TIME_RE.fullmatch(full_time):
        raise ValueError("每日全量搜索时间必须使用 HH:MM 格式")
    return full_time


def normalise_scheduled_search(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return validated scheduled-search settings with safe defaults."""
    source = raw or {}
    return {
        "incremental_enabled": _as_bool(
            source.get("incremental_enabled"),
            bool(DEFAULT_SCHEDULED_SEARCH["incremental_enabled"]),
        ),
        "incremental_interval_minutes": _as_interval(
            source.get(
                "incremental_interval_minutes",
                DEFAULT_SCHEDULED_SEARCH["incremental_interval_minutes"],
            )
        ),
        "full_enabled": _as_bool(
            source.get("full_enabled"),
            bool(DEFAULT_SCHEDULED_SEARCH["full_enabled"]),
        ),
        "full_time": _as_time(
            source.get("full_time", DEFAULT_SCHEDULED_SEARCH["full_time"])
        ),
    }


def launch_agent_dir(home: str | Path | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / "Library" / "LaunchAgents"


def _common_spec(label: str, project_root: Path, log_name: str) -> dict[str, Any]:
    launchd_logs = project_root / "output" / "launchd"
    return {
        "Label": label,
        "WorkingDirectory": str(project_root),
        "ProgramArguments": [str(project_root / "scripts" / "run-jobhunter.sh")],
        "ThrottleInterval": 900,
        "StandardOutPath": str(launchd_logs / f"{log_name}.stdout.log"),
        "StandardErrorPath": str(launchd_logs / f"{log_name}.stderr.log"),
        "ProcessType": "Background",
    }


def launchd_specs(
    settings: Mapping[str, Any], project_root: str | Path,
) -> dict[str, dict[str, Any]]:
    """Build the two launchd plist payloads without touching the user's system."""
    schedule = normalise_scheduled_search(settings)
    root = Path(project_root).resolve()

    incremental = _common_spec(INCREMENTAL_LABEL, root, "incremental")
    incremental["ProgramArguments"].append("--incremental")
    if schedule["incremental_enabled"]:
        incremental["StartInterval"] = schedule["incremental_interval_minutes"] * 60
    else:
        incremental["Disabled"] = True

    hour, minute = (int(part) for part in schedule["full_time"].split(":"))
    full = _common_spec(FULL_LABEL, root, "full")
    full["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    if not schedule["full_enabled"]:
        full["Disabled"] = True

    return {INCREMENTAL_LABEL: incremental, FULL_LABEL: full}


def render_launchd_plists(
    settings: Mapping[str, Any], project_root: str | Path,
) -> dict[str, bytes]:
    """Render XML plist bytes keyed by launchd label."""
    return {
        label: plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)
        for label, payload in launchd_specs(settings, project_root).items()
    }


def _default_runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, **kwargs)


def apply_launchd_schedule(
    settings: Mapping[str, Any],
    project_root: str | Path,
    *,
    home: str | Path | None = None,
    uid: int | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, dict[str, str | bool]]:
    """Write and load the configured launchd jobs.

    This is intentionally explicit: saving the settings page never calls this function.
    The caller must request the "save and apply" action. ``runner`` is injectable so
    offline tests can verify the launchctl commands without touching the user's agents.
    """
    if sys.platform != "darwin":
        raise RuntimeError("macOS launchd 定时任务只能在 macOS 上应用")

    schedule = normalise_scheduled_search(settings)
    root = Path(project_root).resolve()
    agent_dir = launch_agent_dir(home)
    agent_dir.mkdir(parents=True, exist_ok=True)
    runner = runner or _default_runner
    user_id = os.getuid() if uid is None else uid
    domain = f"gui/{user_id}"
    rendered = render_launchd_plists(schedule, root)
    results: dict[str, dict[str, str | bool]] = {}

    for label, content in rendered.items():
        plist_path = agent_dir / f"{label}.plist"
        target = f"{domain}/{label}"
        runner(
            ["launchctl", "bootout", target],
            check=False,
            capture_output=True,
            text=True,
        )
        plist_path.write_bytes(content)

        enabled = (
            schedule["incremental_enabled"]
            if label == INCREMENTAL_LABEL
            else schedule["full_enabled"]
        )
        if enabled:
            result = runner(
                ["launchctl", "bootstrap", domain, str(plist_path)],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "未知错误").strip()
                raise RuntimeError(f"应用 {label} 失败：{detail}")

        results[label] = {"enabled": bool(enabled), "path": str(plist_path)}

    return results
