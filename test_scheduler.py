"""Offline checks for scheduled-search settings and launchd rendering."""
from __future__ import annotations

import plistlib
import subprocess
import tempfile
from pathlib import Path

import scheduler
from scheduler import (
    FULL_LABEL,
    INCREMENTAL_LABEL,
    apply_launchd_schedule,
    launchd_specs,
    normalise_scheduled_search,
    render_launchd_plists,
)


PASS = FAIL = 0


def check(name, got, want=True):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}\n       got={got!r}\n      want={want!r}")


def raises(name, fn, error):
    try:
        fn()
    except error:
        check(name, True)
    except Exception as exc:
        check(name, type(exc).__name__, error.__name__)
    else:
        check(name, False)


print("\n=== Scheduled search settings ===")
defaults = normalise_scheduled_search({})
check("默认增量任务开启", defaults["incremental_enabled"])
check("默认增量间隔", defaults["incremental_interval_minutes"], 180)
check("默认全量任务开启", defaults["full_enabled"])
check("默认全量时间", defaults["full_time"], "02:00")
check(
    "表单值规范化",
    normalise_scheduled_search({
        "incremental_enabled": "on",
        "incremental_interval_minutes": "60",
        "full_enabled": "",
        "full_time": "03:30",
    }),
    {
        "incremental_enabled": True,
        "incremental_interval_minutes": 60,
        "full_enabled": False,
        "full_time": "03:30",
    },
)
raises("过短间隔被拒绝", lambda: normalise_scheduled_search({"incremental_interval_minutes": 10}), ValueError)
raises("错误时间被拒绝", lambda: normalise_scheduled_search({"full_time": "25:00"}), ValueError)

print("\n=== launchd plist rendering ===")
settings = {
    "incremental_enabled": True,
    "incremental_interval_minutes": 60,
    "full_enabled": False,
    "full_time": "03:30",
}
specs = launchd_specs(settings, "/tmp/jobhunter")
check("增量 StartInterval", specs[INCREMENTAL_LABEL]["StartInterval"], 3600)
check("增量参数包含 --incremental", "--incremental" in specs[INCREMENTAL_LABEL]["ProgramArguments"])
check("关闭全量任务写入 Disabled", specs[FULL_LABEL]["Disabled"], True)
rendered = render_launchd_plists(settings, "/tmp/jobhunter")
parsed = plistlib.loads(rendered[FULL_LABEL])
check("渲染结果是 XML plist", parsed["Label"], FULL_LABEL)
check("全量时间仍保留", parsed["StartCalendarInterval"], {"Hour": 3, "Minute": 30})

print("\n=== launchd apply (fake runner) ===")
with tempfile.TemporaryDirectory() as temp_dir:
    calls: list[list[str]] = []

    def fake_runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    original_platform = scheduler.sys.platform
    scheduler.sys.platform = "darwin"
    try:
        result = apply_launchd_schedule(
            settings, "/tmp/jobhunter", home=temp_dir, uid=501, runner=fake_runner,
        )
    finally:
        scheduler.sys.platform = original_platform
    check("应用结果记录增量状态", result[INCREMENTAL_LABEL]["enabled"], True)
    check("应用结果记录全量状态", result[FULL_LABEL]["enabled"], False)
    check(
        "写出两个 plist",
        all(Path(result[label]["path"]).exists() for label in (INCREMENTAL_LABEL, FULL_LABEL)),
        True,
    )
    check("bootstrap 只启动启用的任务", sum(command[1] == "bootstrap" for command in calls), 1)

print(f"\n=== result: {PASS} passed, {FAIL} failed ===")
if FAIL:
    raise SystemExit(1)
