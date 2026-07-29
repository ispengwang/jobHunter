#!/usr/bin/env python3
"""jobhunt-au 本地图形化设置面板。

独立于 Claude 对话运行 —— 起一个本地网页服务,浏览器打开就能用,不需要回到聊天里。

用法:
  pip install -r requirements.txt   # 已含 flask / ruamel.yaml
  python webapp.py
  # 然后浏览器打开 http://127.0.0.1:5050

功能:
  - 改设置:config.yaml 里的搜索词/地点/数据源/签证关键词/LLM provider/打分阈值,
    以及 profile/preferences.md(整份文本编辑,保存直接覆盖文件)
  - 一键运行:抓取 / 抓取+打分 / 用缓存重新打分,后台跑,页面上看实时日志
  - 看结果:output/jobs-ranked.csv 渲染成表格,按分数排序,可按最低分筛选、按公司/职位搜索

config.yaml 用 ruamel.yaml 做“往返”编辑 —— 只改被改动的字段的值,注释和格式保留,
不会因为存了一次盘就把文件里那些解释性注释全冲掉。
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, redirect, render_template_string, request, url_for, jsonify, make_response
from ruamel.yaml import YAML

from candidate_profile import (
    load_or_initialise, profile_form_sections, save_profile, update_from_form,
)
from dashboard import Dashboard, STATUSES
from application_attempts import (
    ApplicationAttempts,
    platform_submission_mode,
)
from application_policy import freshness_bucket
from scrapers.seek_source import clean_seek_title

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "config.yaml"
PREFS_PATH = ROOT / "profile" / "preferences.md"
RANKED_CSV = ROOT / "output" / "jobs-ranked.csv"

yaml = YAML()
yaml.preserve_quotes = True
yaml.indent(mapping=2, sequence=2, offset=0)

app = Flask(__name__)
ACTION_TOKEN = secrets.token_urlsafe(32)
# ---------------------------------------------------------------- 后台运行状态

RUN_STATE = {
    "running": False,
    "mode": None,
    "log": [],
    "returncode": None,
    "started_at": None,
}
RUN_LOCK = threading.Lock()
CONFIG_LOCK = threading.Lock()
MAX_LOG_LINES = 400


def load_config() -> dict:
    with CONFIG_LOCK:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return yaml.load(f)


def save_config(cfg) -> None:
    with CONFIG_LOCK:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f)


def _project_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def _candidate_profile(cfg):
    resume_path = _project_path(cfg["paths"]["resume"])
    prefs_path = _project_path(cfg["paths"]["preferences"])
    resume = resume_path.read_text(encoding="utf-8") if resume_path.exists() else ""
    prefs = prefs_path.read_text(encoding="utf-8") if prefs_path.exists() else ""
    path = _project_path(cfg["paths"].get("candidate_profile", "profile/candidate_profile.md"))
    return path, load_or_initialise(path, resume, prefs, cfg)


def _dashboard(cfg) -> Dashboard:
    return Dashboard(
        _project_path(cfg["paths"].get("dashboard", "data/application-dashboard.csv")),
        _project_path(cfg["paths"].get("application_events", "data/application-events.csv")),
        freshness_config=cfg,
    )


def _attempts(cfg) -> ApplicationAttempts:
    configured_skill = cfg.get("applypilot", {}).get("skill_path", "")
    skill_path = _project_path(configured_skill) if configured_skill else None
    return ApplicationAttempts(
        _project_path(cfg["paths"].get("application_attempts", "data/application-attempts.csv")),
        _dashboard(cfg),
        browser_enabled=bool(cfg.get("application", {}).get("browser_enabled", True)),
        skill_path=skill_path,
        project_root=ROOT,
    )


STATUS_LABELS = {
    "new": "新发现", "review": "待审核", "ready_to_apply": "待投递",
    "applying": "投递中", "needs_user": "需要你处理", "submitted": "已提交",
    "follow_up": "待跟进",
    "skipped": "已跳过", "blocked": "已卡住", "interview": "面试中",
    "rejected": "已拒绝", "withdrawn": "已撤回",
}
USER_STATUSES = ("ready_to_apply", "submitted", "rejected", "interview")
USER_STATUS_LABELS = {
    "ready_to_apply": "待投递",
    "submitted": "已提交",
    "rejected": "被拒绝",
    "interview": "面试中",
}
MODE_LABELS = {"targeted": "精准投递", "broad": "海投", "manual_review": "人工复核"}
FRESHNESS_LABELS = {
    "within_24h": "24 小时内", "within_3d": "3 天内", "older": "较早发布", "unknown": "发布时间未知",
}
ATTEMPT_LABELS = {
    "selected": "已加入投递清单", "queued": "已加入投递清单",
    "browser_opened": "浏览器已打开", "filling": "填写中",
    "needs_user": "需要你处理", "ready_to_submit": "待提交确认", "submitted": "已提交",
    "failed": "失败", "cancelled": "已取消",
}
PLATFORM_MODE_LABELS = {
    "manual_submit": "打开申请页，由你完成平台操作",
    "auto_if_allowed": "ApplyPilot 核验后可自动",
}
def _user_status(status: str) -> str:
    """Project detailed internal execution states onto the four user-facing states."""
    return status if status in {"submitted", "rejected", "interview"} else "ready_to_apply"


def _format_posted_at(value: str, timezone_name: str) -> tuple[str, str]:
    """Return an exact display value and a relative age without inventing missing timezones."""
    raw = (value or "").strip()
    if not raw:
        return "发布时间未知", ""
    if len(raw) == 10:
        try:
            posted_date = datetime.fromisoformat(raw).date()
            today = datetime.now(ZoneInfo(timezone_name)).date()
            days = max(0, (today - posted_date).days)
            relative = "今天发布" if days == 0 else f"{days} 天前"
            return f"{raw}（时间未提供）", relative
        except (ValueError, KeyError):
            return raw, ""
    try:
        posted = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw, ""
    if posted.tzinfo is None:
        return posted.strftime("%Y-%m-%d %H:%M") + "（时区未提供）", ""
    local_zone = ZoneInfo(timezone_name)
    local_posted = posted.astimezone(local_zone)
    age_hours = max(
        0,
        int((datetime.now(timezone.utc) - posted.astimezone(timezone.utc)).total_seconds() // 3600),
    )
    relative = f"{age_hours} 小时前" if age_hours < 48 else f"{age_hours // 24} 天前"
    zone_label = local_posted.tzname() or timezone_name
    return local_posted.strftime("%Y-%m-%d %H:%M") + f" {zone_label}", relative


def _format_sync_at(value: str, timezone_name: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return "尚无同步记录"
    try:
        timestamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local = timestamp.astimezone(ZoneInfo(timezone_name))
    return f"{local:%Y-%m-%d %H:%M} {local.tzname() or timezone_name}"


def _latest_sync_info(
    rows: list[dict[str, str]], timezone_name: str,
) -> tuple[str, str, int]:
    """Return the latest scoring run marker, display time, and number of rows it touched."""
    synced_rows = [row for row in rows if row.get("last_synced_at", "").strip()]
    candidates = synced_rows or [row for row in rows if row.get("last_updated_at", "").strip()]
    if not candidates:
        return "", "尚无同步记录", 0
    marker_key = "last_synced_at" if synced_rows else "last_updated_at"
    latest = max(candidates, key=lambda row: row.get(marker_key, ""))
    marker = latest.get(marker_key, "")
    run_id = latest.get("last_run_id", "")
    if run_id:
        count = sum(1 for row in candidates if row.get("last_run_id") == run_id)
    else:
        count = sum(1 for row in candidates if row.get(marker_key) == marker)
    return run_id, _format_sync_at(marker, timezone_name), count


def _scoring_rules_path(cfg) -> Path:
    skill_path = (cfg.get("applypilot", {}).get("skill_path") or "").strip()
    if not skill_path:
        raise FileNotFoundError("config.yaml 未配置 applypilot.skill_path")
    return _project_path(skill_path) / "references" / "deepseek-scoring-rules.md"


def _rules_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]


def _load_scoring_rules(cfg) -> tuple[Path, str, str]:
    path = _scoring_rules_path(cfg)
    if not path.exists():
        raise FileNotFoundError(f"找不到评分规则：{path}")
    content = path.read_text(encoding="utf-8")
    return path, content, _rules_hash(content)


def _validate_scoring_rules(content: str) -> None:
    if len(content.strip()) < 500:
        raise ValueError("评分规则过短；请保留完整的评分维度、封顶条件和输出合同")
    required = {"id", "score", "summary", "reason", "matched", "missing", "sponsorship_signal"}
    missing = sorted(field for field in required if field not in content)
    if missing:
        raise ValueError("评分规则缺少 DeepSeek 输出字段：" + "、".join(missing))


def _extract_score_dimensions(content: str) -> list[dict[str, str]]:
    dimensions: list[dict[str, str]] = []
    for line in content.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 2 or not re.fullmatch(r"\d+(?:[–-]\d+)?", cells[1]):
            continue
        dimensions.append({"name": cells[0], "points": cells[1]})
    return dimensions


def _save_scoring_rules(cfg, content: str, expected_hash: str) -> tuple[str, Path]:
    path, current, current_hash = _load_scoring_rules(cfg)
    if expected_hash and expected_hash != current_hash:
        raise ValueError("评分规则已被其他进程修改，请刷新页面后再保存")
    _validate_scoring_rules(content)
    backups = _project_path(
        cfg.get("paths", {}).get("scoring_rule_backups", "data/scoring-rule-backups")
    )
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backups / f"{stamp}-{current_hash}.md"
    backup.write_text(current, encoding="utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content.rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)
    return _rules_hash(content.rstrip() + "\n"), backup


def _ranked_lookup(path: Path | None = None) -> dict[tuple[str, ...], dict[str, str]]:
    """Load result-only fields so the unified Dashboard keeps the result-page context."""
    ranked_path = path or RANKED_CSV
    if not ranked_path.exists():
        return {}
    with ranked_path.open(encoding="utf-8", newline="") as f:
        ranked = list(csv.DictReader(f))
    lookup: dict[tuple[str, ...], dict[str, str]] = {}
    for row in ranked:
        url = (row.get("url") or "").strip()
        title = (row.get("title") or "").strip().lower()
        company = (row.get("company") or "").strip().lower()
        source = (row.get("source") or "").strip().lower()
        if url:
            lookup[("url", url)] = row
        if title and company:
            lookup[("job", title, company, source)] = row
    return lookup


def _unified_dashboard_rows(
    rows: list[dict[str, str]],
    today_ids: set[str],
    attempts_by_job: dict[str, dict[str, str]],
    timezone_name: str = "Australia/Melbourne",
    freshness_config: dict | None = None,
    now: datetime | None = None,
    latest_run_id: str = "",
    ranked_path: Path | None = None,
) -> list[dict[str, str]]:
    """Decorate Dashboard rows for the result-style unified UI without changing CSV data."""
    ranked = _ranked_lookup(ranked_path)
    prepared: list[dict[str, str]] = []
    freshness_order = {"within_24h": 0, "within_3d": 1, "older": 2, "unknown": 3}

    for source_row in rows:
        row = dict(source_row)
        url = (row.get("url") or "").strip()
        title = (row.get("title") or "").strip().lower()
        company = (row.get("company") or "").strip().lower()
        source = (row.get("source") or "").strip().lower()
        result = ranked.get(("url", url)) or ranked.get(("job", title, company, source))
        if result:
            row["score"] = row.get("job_fit_score") or row.get("match_score") or result.get("score", "")
            row["reason"] = row.get("job_fit_reason") or result.get("reason", "")
            row["summary"] = row.get("job_summary") or result.get("summary", "")
            row["posted_at"] = row.get("posted_at") or result.get("posted_date", "")
            row["salary_raw"] = result.get("salary_raw", "")
            row["matched"] = result.get("matched", "")
            row["missing"] = result.get("missing", "")
        else:
            row["score"] = row.get("job_fit_score") or row.get("match_score") or ""
            row["reason"] = row.get("job_fit_reason", "")
            row["summary"] = row.get("job_summary", "")

        score = _score_int(row)
        if source == "seek":
            row["title"] = clean_seek_title(row.get("title", ""))
        status = row.get("status") or "review"
        display_status = _user_status(status)
        freshness = (
            freshness_bucket(row.get("posted_at"), freshness_config, now)
            if freshness_config is not None
            else row.get("freshness_bucket") or "unknown"
        )
        posted_display, posted_relative = _format_posted_at(
            row.get("posted_at", ""), timezone_name
        )
        attempt = attempts_by_job.get(row.get("job_id", ""), {})
        platform_mode = platform_submission_mode(row.get("source", ""), row.get("url", ""))
        attempt_status = attempt.get("status", "")
        apply_button_label = "加入投递清单"
        apply_explanation = (
            "点击后只记录到本地投递清单，不会启动后台进程或打开招聘网站；"
            "当前 Agent 可通过 --handoff-list 读取。"
        )
        if row.get("next_action") == "Run the queued handoff with the applypilot-au skill":
            row["next_action"] = (
                "Current Agent will continue automatically"
                if platform_mode == "auto_if_allowed"
                else "Platform policy requires manual submission"
            )
        row.update({
            # The UI range starts at -1 so unscored rows stay visible at the bottom.
            "score_int": str(score if score >= 0 else -1),
            "score_label": f"{score} 分" if score >= 0 else "待人工评分",
            "score_class": "high" if score >= 80 else "mid" if score >= 60 else "low",
            "display_status": display_status,
            "status_label": USER_STATUS_LABELS[display_status],
            "mode_label": MODE_LABELS.get(row.get("application_mode", ""), row.get("application_mode") or "人工复核"),
            "freshness_label": FRESHNESS_LABELS.get(freshness, freshness),
            "freshness_bucket": freshness,
            "posted_display": posted_display,
            "posted_relative": posted_relative,
            "posted_at_raw": row.get("posted_at", ""),
            "source_label": (row.get("source") or "unknown").upper(),
            "today_action": "1" if row.get("job_id") in today_ids else "0",
            "latest_run": "1" if latest_run_id and row.get("last_run_id") == latest_run_id else "0",
            "attempt_status": attempt_status,
            "attempt_status_label": ATTEMPT_LABELS.get(attempt_status, attempt_status),
            "attempt_id": attempt.get("attempt_id", ""),
            "attempt_readiness": attempt.get("readiness", ""),
            "platform_mode": platform_mode,
            "platform_mode_label": PLATFORM_MODE_LABELS.get(platform_mode, platform_mode),
            "apply_button_label": apply_button_label,
            "apply_explanation": apply_explanation,
            "freshness_order": str(freshness_order.get(freshness, 9)),
        })
        prepared.append(row)

    prepared.sort(key=lambda r: (-_score_int(r), int(r.get("freshness_order", "9")), r.get("title", "").lower()))
    return prepared


def _provider_key_env_name(provider: str) -> str:
    return {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }.get(provider, "ANTHROPIC_API_KEY")


def _run_pipeline(cmd: list[str], env_overrides: dict) -> None:
    env = os.environ.copy()
    env.update({k: v for k, v in env_overrides.items() if v})

    with RUN_LOCK:
        RUN_STATE["running"] = True
        RUN_STATE["log"] = [f"$ {' '.join(cmd)}"]
        RUN_STATE["returncode"] = None
        RUN_STATE["started_at"] = time.strftime("%H:%M:%S")

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            with RUN_LOCK:
                RUN_STATE["log"].append(line.rstrip("\n"))
                if len(RUN_STATE["log"]) > MAX_LOG_LINES:
                    RUN_STATE["log"] = RUN_STATE["log"][-MAX_LOG_LINES:]
        proc.wait()
        with RUN_LOCK:
            RUN_STATE["returncode"] = proc.returncode
    except Exception as e:
        with RUN_LOCK:
            RUN_STATE["log"].append(f"[启动失败] {e}")
            RUN_STATE["returncode"] = -1
    finally:
        with RUN_LOCK:
            RUN_STATE["running"] = False


def _attempt_public_payload(
    attempt: dict[str, str],
    *,
    message: str = "",
) -> dict[str, str | int | bool | None]:
    """Return browser-safe attempt state without local profile/resume paths."""
    status = attempt.get("status", "")
    platform_mode = platform_submission_mode(
        attempt.get("platform", ""), attempt.get("url", "")
    )
    return {
        "ok": True,
        "attempt_id": attempt["attempt_id"],
        "attempt_status": status,
        "attempt_status_label": ATTEMPT_LABELS.get(status, status),
        "platform_mode": platform_mode,
        "platform_mode_label": PLATFORM_MODE_LABELS.get(platform_mode, platform_mode),
        "message": message,
    }


# ---------------------------------------------------------------- 页面

BASE_STYLE = """
  :root {
    color-scheme: light dark;
    --bg: #f7f7f4; --surface: #ffffff; --border: #e6e4dc; --border-strong: #d6d4c9;
    --text: #1c1c19; --text-dim: #6e6e66; --text-faint: #98978c;
    --accent: #1f6f5b; --accent-hover: #185543; --accent-text: #ffffff;
    --accent-soft: #edf6f2; --accent-border: #b8d9cd;
    --success-bg: #eaf3de; --success-text: #3b6d11; --success-border: #c0dd97;
    --warn-bg: #faeeda; --warn-text: #854f0b; --warn-border: #f0cf94;
    --danger-bg: #fcebeb; --danger-text: #a32d2d; --danger-border: #f0b8b8;
    --radius: 10px; --radius-lg: 14px;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #15150f; --surface: #201f1a; --border: #35342c; --border-strong: #46453a;
      --text: #ececea; --text-dim: #a8a79c; --text-faint: #75746a;
      --accent: #8bd3b6; --accent-hover: #a8e2c8; --accent-text: #10231c;
      --accent-soft: #17342a; --accent-border: #2d624d;
      --success-bg: #17340a; --success-text: #a6d97a; --success-border: #2a5011;
      --warn-bg: #412402; --warn-text: #f0b25a; --warn-border: #63380a;
      --danger-bg: #2c0f0f; --danger-text: #f09595; --danger-border: #501313;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 860px; margin: 0 auto; padding: 0 20px 64px;
    background: var(--bg); color: var(--text); line-height: 1.5;
  }
  header.top { position: sticky; top: 0; background: var(--bg); padding: 22px 0 14px;
               display: flex; align-items: baseline; justify-content: space-between; z-index: 5; }
  header.top h1 { font-size: 18px; font-weight: 600; margin: 0; letter-spacing: -0.01em; }
  nav.tabs { display: flex; gap: 4px; }
  nav.tabs a { font-size: 13px; padding: 6px 12px; border-radius: 100px; text-decoration: none;
               color: var(--text-dim); }
  nav.tabs a.active { background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-border); }
  p.sub { color: var(--text-dim); font-size: 13px; margin: -8px 0 20px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
          padding: 20px 22px; margin: 14px 0; }
  .card h2 { font-size: 14px; font-weight: 600; margin: 0 0 4px; }
  .card .hint { font-size: 12.5px; color: var(--text-dim); margin: 0 0 14px; }
  label { display: block; font-size: 12.5px; color: var(--text-dim); margin: 12px 0 5px; font-weight: 500; }
  label:first-of-type { margin-top: 0; }
  input[type=text], input[type=number], input[type=password], select, textarea {
    width: 100%; padding: 8px 11px; font-size: 14px; font-family: inherit;
    border: 1px solid var(--border-strong); border-radius: var(--radius);
    background: var(--surface); color: var(--text); transition: border-color .15s;
  }
  input:focus, select:focus, textarea:focus {
    outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 16%, transparent);
  }
  textarea { resize: vertical; line-height: 1.5; }
  .row { display: grid; gap: 12px; }
  .row.cols-2 { grid-template-columns: 1fr 1fr; }
  .checks { display: flex; gap: 20px; flex-wrap: wrap; margin-top: 4px; }
  .checks label { display: flex; align-items: center; gap: 7px; font-size: 13.5px;
                  color: var(--text); margin: 0; font-weight: 400; }
  .checks input[type=checkbox] { width: 15px; height: 15px; accent-color: var(--accent); }
  .btn { display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px;
         border-radius: var(--radius); border: 1px solid var(--border-strong);
         background: var(--surface); color: var(--text); font-size: 13.5px; font-weight: 500;
         cursor: pointer; text-decoration: none; transition: background .1s, transform .05s; }
  .btn:hover { background: var(--accent-soft); border-color: var(--accent-border); }
  .btn:active { transform: scale(.98); }
  .btn.primary { background: var(--accent); color: var(--accent-text); border-color: var(--accent); }
  .btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
  .btn[disabled] { opacity: .4; cursor: not-allowed; }
  .flash { background: var(--success-bg); border: 1px solid var(--success-border); color: var(--success-text);
           border-radius: var(--radius); padding: 10px 14px; font-size: 13px; margin: 4px 0 18px; }
  .pill { display: inline-flex; align-items: center; border: 1px solid var(--accent-border);
          border-radius: 100px; padding: 2px 8px; font-size: 11px;
          background: var(--accent-soft); color: var(--accent); }
  .runbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 14px; }
  pre.log { background: #0c0c0a; color: #b9e6b0; font-size: 12px; line-height: 1.6;
            padding: 14px; border-radius: var(--radius); max-height: 320px; overflow-y: auto;
            white-space: pre-wrap; word-break: break-all; margin-top: 14px; }
  .status { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px;
            padding: 4px 10px 4px 8px; border-radius: 100px; background: var(--bg);
            border: 1px solid var(--border); color: var(--text-dim); }
  .status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-faint); }
  .status.running .dot { background: #e0a020; }
  .status.done .dot { background: #3b9950; }
  .status.error .dot { background: #d05050; }
  footer.hint { font-size: 12px; color: var(--text-faint); margin-top: 28px; text-align: center; }
  @media (max-width:600px) {
    body { padding: 0 14px 48px; }
    header.top { align-items: flex-start; flex-direction: column; gap: 8px; padding: 14px 0 10px; }
    nav.tabs { width: 100%; overflow-x: auto; flex-wrap: nowrap; padding-bottom: 2px;
               scrollbar-width: none; }
    nav.tabs::-webkit-scrollbar { display: none; }
    nav.tabs a { flex: 0 0 auto; white-space: nowrap; }
    p.sub { margin-top: 0; }
    .card { padding: 16px; }
  }
"""

INDEX_HTML = """
<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>jobhunt-au 设置面板</title>
<style>""" + BASE_STYLE + """</style>
</head><body>

<header class="top">
  <h1>jobhunt-au</h1>
  <nav class="tabs">
    <a href="/" class="active">设置</a>
    <a href="/profile">档案</a>
    <a href="/dashboard">Dashboard</a>
  </nav>
</header>
<p class="sub">改设置、一键跑抓取和打分,不用回终端。</p>

{% if saved %}<div class="flash">已保存到 config.yaml 和 profile/preferences.md。</div>{% endif %}

<form method="post" action="/save">

  <div class="card">
    <h2>搜索条件</h2>
    <label>搜索词(每行一个)</label>
    <textarea name="terms" rows="5">{{ terms }}</textarea>
    <div class="row cols-2">
      <div><label>地点</label><input type="text" name="location" value="{{ cfg.search.location }}"></div>
      <div><label>岗位新鲜度</label>
        <select name="hours_old">
          <option value="168" {{ 'selected' if cfg.search.hours_old == 168 }}>7 天内</option>
          <option value="336" {{ 'selected' if cfg.search.hours_old == 336 }}>14 天内</option>
          <option value="720" {{ 'selected' if cfg.search.hours_old == 720 }}>30 天内</option>
        </select>
      </div>
    </div>
    <label>数据源</label>
    <div class="checks">
      <label><input type="checkbox" name="src_linkedin" {{ 'checked' if cfg.sources.linkedin }}>LinkedIn</label>
      <label><input type="checkbox" name="src_indeed" {{ 'checked' if cfg.sources.indeed }}>Indeed</label>
      <label><input type="checkbox" name="src_seek" {{ 'checked' if cfg.sources.seek }}>SEEK</label>
    </div>
  </div>

  <div class="card">
    <h2>签证过滤</h2>
    <label>命中即淘汰的关键词(每行一个)</label>
    <textarea name="exclude_keywords" rows="4">{{ exclude_keywords }}</textarea>
    <label>命中即加分的关键词(每行一个)</label>
    <textarea name="bonus_keywords" rows="3">{{ bonus_keywords }}</textarea>
  </div>

  <div class="card">
    <h2>AI 打分</h2>
    <div class="row cols-2">
      <div><label>LLM provider</label>
        <select name="provider">
          <option value="anthropic" {{ 'selected' if cfg.llm.provider == 'anthropic' }}>Anthropic</option>
          <option value="openai" {{ 'selected' if cfg.llm.provider == 'openai' }}>OpenAI</option>
          <option value="deepseek" {{ 'selected' if cfg.llm.provider == 'deepseek' }}>DeepSeek(便宜)</option>
        </select>
      </div>
      <div><label>生成材料分数门槛(--generate 时生效)</label>
        <input type="number" name="threshold" min="0" max="100" step="5" value="{{ cfg.scoring.generate_threshold }}">
      </div>
    </div>
  </div>

  <div class="card">
    <h2>求职偏好</h2>
    <p class="hint">来自 profile/preferences.md,整份编辑 —— 签证状态、薪资预期、seniority、技术栈偏好都在这里,保存会整份覆盖原文件。</p>
    <textarea name="preferences" rows="16" style="font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12.5px;">{{ preferences }}</textarea>
  </div>

  <button type="submit" class="btn primary">保存设置</button>
</form>

<div class="card">
  <h2>运行</h2>
  <p class="hint">跑的时候这台电脑要保持开着、网络要通。抓取通常 5-15 分钟,打分几分钟到十几分钟不等,取决于岗位数量和 provider。</p>

  <label>API key(可选 —— 不填就用启动这个网页前 export 过的环境变量;这里填了只在这次运行时用,不会写进任何文件)</label>
  <input type="password" id="api_key" placeholder="sk-...">

  <label style="margin-top: 12px;">数量上限(只处理前 N 个,抓取和打分都更快;留空/0 = 全部)</label>
  <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
    <input type="number" id="limit_n" min="0" step="10" value="50" style="width:90px;">
    <span class="seg" id="seg-limit">
      <button type="button" data-v="20" onclick="setLimit(20,this)">20</button>
      <button type="button" data-v="50" class="on" onclick="setLimit(50,this)">50</button>
      <button type="button" data-v="100" onclick="setLimit(100,this)">100</button>
      <button type="button" data-v="0" onclick="setLimit(0,this)">全部</button>
    </span>
  </div>

  <div class="checks" style="margin-top: 12px;">
    <label><input type="checkbox" id="opt_generate"> 同时生成简历/cover letter(--generate,额外花钱,仅对"抓取+打分"和"用缓存重新打分"生效)</label>
  </div>

  <div class="runbar">
    <button type="button" class="btn" id="btn-scrape" onclick="startRun('scrape')">只抓取</button>
    <button type="button" class="btn primary" id="btn-full" onclick="startRun('full')">抓取 + 打分</button>
    <button type="button" class="btn" id="btn-rescore" onclick="startRun('rescore')">用缓存重新打分</button>
    <span class="status" id="status-pill"><span class="dot"></span><span id="status-text">{{ '运行中' if run_state.running else '空闲' }}</span></span>
  </div>

  <pre class="log" id="log">{{ log_text }}</pre>
</div>

<footer class="hint">只在本机监听,不对外网开放。</footer>

<script>
const runButtons = ['btn-scrape', 'btn-full', 'btn-rescore'];

function setButtonsDisabled(disabled) {
  runButtons.forEach(id => document.getElementById(id).disabled = disabled);
}

function setStatus(cls, text) {
  const pill = document.getElementById('status-pill');
  pill.className = 'status ' + cls;
  document.getElementById('status-text').textContent = text;
}

function setLimit(n, btn) {
  document.getElementById('limit_n').value = n;
  btn.parentNode.querySelectorAll('button').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
}

async function startRun(mode) {
  const apiKey = document.getElementById('api_key').value;
  const generate = document.getElementById('opt_generate').checked;
  const limit = parseInt(document.getElementById('limit_n').value || '0', 10) || 0;
  const resp = await fetch('/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode, api_key: apiKey, generate, limit}),
  });
  const data = await resp.json();
  if (!resp.ok) { alert(data.error || '启动失败'); return; }
  poll();
}

async function poll() {
  const resp = await fetch('/run/status');
  const data = await resp.json();
  document.getElementById('log').textContent = data.log.join('\\n');
  if (data.running) {
    setStatus('running', '运行中');
  } else if (data.returncode === 0) {
    setStatus('done', '完成');
  } else if (data.returncode !== null) {
    setStatus('error', '出错(退出码 ' + data.returncode + ')');
  } else {
    setStatus('', '空闲');
  }
  setButtonsDisabled(data.running);
  const log = document.getElementById('log');
  log.scrollTop = log.scrollHeight;
  if (data.running) setTimeout(poll, 1500);
}

setButtonsDisabled({{ 'true' if run_state.running else 'false' }});
if ({{ 'true' if run_state.running else 'false' }}) poll();
</script>

</body></html>
"""

RESULTS_HTML = """
<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>打分结果 · jobhunt-au</title>
<style>""" + BASE_STYLE + """
  .stats { display: flex; gap: 8px; margin: 4px 0 16px; flex-wrap: wrap; }
  .stat { flex: 1; min-width: 90px; background: var(--surface); border: 1px solid var(--border);
          border-radius: var(--radius); padding: 10px 12px; }
  .stat .n { font-size: 20px; font-weight: 600; line-height: 1.2; }
  .stat .k { font-size: 11.5px; color: var(--text-dim); margin-top: 2px; }
  .stat.high .n { color: var(--success-text); }
  .stat.mid .n { color: var(--warn-text); }
  .stat.low .n { color: var(--danger-text); }

  .filters { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
             padding: 14px 16px; margin-bottom: 16px; display: flex; flex-direction: column; gap: 12px; }
  .filter-row { display: flex; gap: 16px; align-items: center; flex-wrap: wrap; }
  .filter-row label { display: flex; align-items: center; gap: 8px; font-size: 12.5px;
                      color: var(--text-dim); margin: 0; font-weight: 500; white-space: nowrap; }
  .filters input, .filters select { width: auto; padding: 7px 10px; }
  #q { width: 200px; }
  #min-score { width: 52px; text-align: center; }
  #min-range { width: 130px; accent-color: var(--text); }
  .seg { display: inline-flex; border: 1px solid var(--border-strong); border-radius: var(--radius);
         overflow: hidden; }
  .seg button { border: 0; background: var(--surface); color: var(--text-dim); font-size: 12.5px;
                padding: 6px 11px; cursor: pointer; border-right: 1px solid var(--border); }
  .seg button:last-child { border-right: 0; }
  .seg button.on { background: var(--accent); color: var(--accent-text); }
  .count { font-size: 12.5px; color: var(--text-dim); margin-left: auto; }
  .btn-reset { font-size: 12px; color: var(--text-dim); background: none; border: 0; cursor: pointer;
               text-decoration: underline; padding: 0; }

  .row-item { padding: 15px 18px; }
  .row-head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .badge { font-size: 13px; font-weight: 600; padding: 2px 10px; border-radius: 100px;
           border: 1px solid var(--border); flex-shrink: 0; }
  .badge.high { background: var(--success-bg); color: var(--success-text); border-color: var(--success-border); }
  .badge.mid { background: var(--warn-bg); color: var(--warn-text); border-color: var(--warn-border); }
  .badge.low { background: var(--danger-bg); color: var(--danger-text); border-color: var(--danger-border); }
  .row-title { font-weight: 600; font-size: 14.5px; }
  .row-company { color: var(--text-dim); font-size: 14.5px; }
  .chip { font-size: 11px; padding: 1px 8px; border-radius: 100px; border: 1px solid var(--border); flex-shrink: 0; }
  .chip.yes { background: var(--success-bg); color: var(--success-text); border-color: var(--success-border); }
  .chip.no { background: var(--danger-bg); color: var(--danger-text); border-color: var(--danger-border); }
  .chip.unknown { color: var(--text-faint); }
  .source-tag { font-size: 10.5px; text-transform: uppercase; letter-spacing: .03em;
                color: var(--text-faint); border: 1px solid var(--border); border-radius: 5px;
                padding: 1px 6px; flex-shrink: 0; }
  .meta { font-size: 12.5px; color: var(--text-dim); margin: 7px 0 0; }
  .reason { font-size: 13px; margin-top: 7px; }
  .tags { margin-top: 7px; display: flex; gap: 6px; flex-wrap: wrap; }
  .tag { font-size: 11px; padding: 1px 7px; border-radius: 5px; }
  .tag.m { background: var(--success-bg); color: var(--success-text); }
  .tag.x { background: var(--bg); color: var(--text-dim); border: 1px solid var(--border); }
  a.open { font-size: 12.5px; margin-top: 9px; display: inline-block; color: var(--text); }
  .no-match { color: var(--text-dim); font-size: 14px; margin-top: 30px; text-align: center; }
  .empty { color: var(--text-dim); font-size: 14px; margin-top: 40px; text-align: center; }
"""

RESULTS_HTML += """
</style>
</head><body>

<header class="top">
  <h1>jobhunt-au</h1>
  <nav class="tabs">
    <a href="/">设置</a>
    <a href="/profile">档案</a>
    <a href="/dashboard">Dashboard</a>
    <a href="/dashboard" class="active">Dashboard</a>
  </nav>
</header>

{% if not rows %}
<p class="empty">还没有结果 —— 先在设置面板里跑一次"抓取 + 打分"。</p>
{% else %}

<div class="stats">
  <div class="stat"><div class="n">{{ stats.total }}</div><div class="k">总岗位</div></div>
  <div class="stat high"><div class="n">{{ stats.high }}</div><div class="k">80+ 高匹配</div></div>
  <div class="stat mid"><div class="n">{{ stats.mid }}</div><div class="k">60-79 可考虑</div></div>
  <div class="stat"><div class="n">{{ stats.can_sponsor }}</div><div class="k">明确可担保</div></div>
</div>

<div class="filters">
  <div class="filter-row">
    <label>最低分
      <input type="range" id="min-range" min="0" max="100" step="5" value="0" oninput="syncRange()">
      <input type="number" id="min-score" min="0" max="100" value="0" oninput="syncNumber()">
    </label>
    <label>搜索 <input type="text" id="q" placeholder="公司 / 职位 / 关键词" oninput="filterRows()"></label>
  </div>
  <div class="filter-row">
    <label>担保
      <span class="seg" id="seg-sponsor">
        <button class="on" data-v="all" onclick="setSeg('sponsor',this)">全部</button>
        <button data-v="explicit_yes" onclick="setSeg('sponsor',this)">可担保</button>
        <button data-v="unknown" onclick="setSeg('sponsor',this)">未提及</button>
        <button data-v="explicit_no" onclick="setSeg('sponsor',this)">不担保</button>
      </span>
    </label>
    <label>来源
      <span class="seg" id="seg-source">
        <button class="on" data-v="all" onclick="setSeg('source',this)">全部</button>
        {% for s in sources %}<button data-v="{{ s }}" onclick="setSeg('source',this)">{{ s }}</button>{% endfor %}
      </span>
    </label>
    <span class="count" id="count"></span>
  </div>
  <div class="filter-row">
    <button class="btn-reset" onclick="resetFilters()">重置筛选</button>
  </div>
</div>

<div id="rows">
{% for r in rows %}
{% set s = r.score|int(-999) %}
{% set sig = r.sponsorship_signal %}
<div class="card row-item"
     data-score="{{ r.score }}"
     data-text="{{ (r.title ~ ' ' ~ r.company ~ ' ' ~ r.reason ~ ' ' ~ r.matched)|lower }}"
     data-sponsor="{{ sig }}"
     data-source="{{ r.source }}">
  <div class="row-head">
    <span class="badge {{ 'high' if s >= 80 else ('mid' if s >= 60 else 'low') }}">{{ r.score }} 分</span>
    <span class="row-title">{{ r.title }}</span>
    <span class="row-company">@ {{ r.company }}</span>
    {% if sig == 'explicit_yes' %}<span class="chip yes">可担保</span>
    {% elif sig == 'explicit_no' %}<span class="chip no">不担保</span>
    {% else %}<span class="chip unknown">担保未提及</span>{% endif %}
    <span class="source-tag">{{ r.source }}</span>
  </div>
  <div class="meta">{{ r.location or '未列出' }} · {{ r.salary_raw or '薪资未列出' }}</div>
  <div class="reason">{{ r.reason }}</div>
  <div class="tags">
    {% for m in (r.matched or '').split(',') if m.strip() %}<span class="tag m">{{ m.strip() }}</span>{% endfor %}
    {% for x in (r.missing or '').split(',') if x.strip() %}<span class="tag x">缺 {{ x.strip() }}</span>{% endfor %}
  </div>
  <a class="open" href="{{ r.url }}" target="_blank" rel="noopener">打开链接 →</a>
</div>
{% endfor %}
</div>
<p class="no-match" id="no-match" style="display:none">没有符合筛选条件的岗位。</p>

<script>
const state = { sponsor: 'all', source: 'all' };

function syncRange() {
  document.getElementById('min-score').value = document.getElementById('min-range').value;
  filterRows();
}
function syncNumber() {
  let v = parseInt(document.getElementById('min-score').value || '0', 10);
  v = Math.max(0, Math.min(100, isNaN(v) ? 0 : v));
  document.getElementById('min-range').value = v;
  filterRows();
}
function setSeg(group, btn) {
  state[group] = btn.dataset.v;
  btn.parentNode.querySelectorAll('button').forEach(b => b.classList.remove('on'));
  btn.classList.add('on');
  filterRows();
}
function resetFilters() {
  document.getElementById('min-score').value = 0;
  document.getElementById('min-range').value = 0;
  document.getElementById('q').value = '';
  state.sponsor = 'all'; state.source = 'all';
  document.querySelectorAll('.seg').forEach(seg => {
    seg.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === 'all'));
  });
  filterRows();
}
function filterRows() {
  const min = parseInt(document.getElementById('min-score').value || '0', 10);
  const q = document.getElementById('q').value.toLowerCase().trim();
  let shown = 0;
  document.querySelectorAll('.row-item').forEach(el => {
    const score = parseInt(el.dataset.score, 10);
    const ok = score >= min
      && el.dataset.text.includes(q)
      && (state.sponsor === 'all' || el.dataset.sponsor === state.sponsor)
      && (state.source === 'all' || el.dataset.source === state.source);
    el.style.display = ok ? '' : 'none';
    if (ok) shown++;
  });
  document.getElementById('count').textContent = '显示 ' + shown + ' / {{ rows|length }} 条';
  document.getElementById('no-match').style.display = shown === 0 ? '' : 'none';
}
filterRows();
</script>
{% endif %}

</body></html>
"""

PROFILE_HTML = """
<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Candidate Profile · jobhunt-au</title><style>""" + BASE_STYLE + """</style>
</head><body>
<header class="top"><h1>Candidate Profile</h1><nav class="tabs">
  <a href="/">设置</a><a href="/profile" class="active">档案</a><a href="/dashboard">Dashboard</a>
</nav></header>
<p class="sub">初次由现有简历自动填充。这里的编辑会成为本地事实来源，后续运行不会把它覆盖掉。</p>
{% if saved %}<div class="flash">Candidate Profile 已保存。</div>{% endif %}
{% if missing %}<div class="card"><h2>真实投递前仍需补全</h2><p class="hint">{{ missing|join('、') }}。缺失字段会保持 unknown，不会被系统猜测。</p></div>{% endif %}
<form method="post" action="/profile/save">
{% for title, fields in sections %}
<div class="card"><h2>{{ title }}</h2>
  <div class="row cols-2">
  {% for field in fields %}
    <div {% if field.multiline %}style="grid-column:1/-1"{% endif %}>
      <label>{{ field.label }}</label>
      {% if field.multiline %}<textarea name="{{ field.name }}" rows="3">{{ field.value }}</textarea>
      {% else %}<input type="text" name="{{ field.name }}" value="{{ field.value }}">{% endif %}
    </div>
  {% endfor %}
  </div>
</div>
{% endfor %}
<button class="btn primary" type="submit">保存 Candidate Profile</button>
</form>
</body></html>
"""

SCORING_RULES_HTML = """
<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepSeek 评分规则 · jobhunt-au</title><style>""" + BASE_STYLE + """
  .rules-editor { min-height:65vh; font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
                  font-size:12.5px; line-height:1.55; }
  .rule-meta { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
</style></head><body>
<header class="top"><h1>DeepSeek 评分规则</h1><nav class="tabs">
  <a href="/">设置</a><a href="/profile">档案</a><a href="/scoring-rules" class="active">评分规则</a><a href="/dashboard">Dashboard</a>
</nav></header>
<p class="sub">这是 ApplyPilot Skill 的实际评分合同。下一次运行会把这里的完整内容交给 DeepSeek；Agent 不会自行生成分数或静默修改规则。</p>
{% if saved %}<div class="flash">评分规则已保存；旧版本已自动备份。新规则将在下一次评分时生效。</div>{% endif %}
{% if error %}<div class="card"><h2>无法保存</h2><p class="hint">{{ error }}</p></div>{% endif %}
<div class="card">
  <div class="rule-meta"><span class="pill">sha256: {{ rules_hash }}</span><span class="hint">规则文件：{{ rules_path }}</span></div>
  <p class="hint">你可以调整权重、分数区间、封顶条件和缺口处理方式。请保留底部 JSON 输出字段，否则系统无法读取 DeepSeek 结果。</p>
</div>
<form method="post" action="/scoring-rules/save">
  <input type="hidden" name="expected_hash" value="{{ rules_hash }}">
  <textarea class="rules-editor" name="content" spellcheck="false">{{ content }}</textarea>
  <p><button class="btn primary" type="submit">保存并启用新规则</button> <a class="btn" href="/dashboard">返回 Dashboard</a></p>
</form>
</body></html>
"""

DASHBOARD_HTML = """
<!doctype html>
<html lang="zh"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard · jobhunt-au</title><style>""" + BASE_STYLE + """
  .stats { display:grid; grid-template-columns:repeat(6,1fr); gap:8px; margin:4px 0 16px; }
  .stat { min-width:0; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); padding:10px 12px; }
  .stat .n { font-size:20px; font-weight:600; line-height:1.2; }
  .stat .k { color:var(--text-dim); font-size:11.5px; margin-top:2px; white-space:nowrap; }
  .stat.high .n { color:var(--success-text); }.stat.mid .n { color:var(--warn-text); }.stat.danger .n { color:var(--danger-text); }
  .today-banner { display:flex; align-items:center; justify-content:space-between; gap:12px; margin:4px 0 16px; padding:12px 14px; border:1px solid var(--accent-border); border-radius:var(--radius-lg); background:var(--accent-soft); }
  .today-banner strong { display:block; font-size:13px; color:var(--accent); }.today-banner span { color:var(--text-dim); font-size:12px; }
  .today-banner .btn { white-space:nowrap; flex-shrink:0; }
  .rules-card { display:grid; grid-template-columns:minmax(180px,.7fr) minmax(0,2fr); gap:18px; }
  .rules-card h2 { margin-bottom:5px; }.rule-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px 14px; }
  .rule-dimension { display:flex; justify-content:space-between; gap:8px; padding:5px 0; border-bottom:1px solid var(--border); font-size:12px; }
  .rule-dimension b { color:var(--accent); white-space:nowrap; }
  .filters { background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-lg); padding:14px 16px; margin-bottom:16px; display:flex; flex-direction:column; gap:12px; }
  .filter-row { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
  .filter-row label { display:flex; align-items:center; gap:8px; font-size:12.5px; color:var(--text-dim); margin:0; font-weight:500; white-space:nowrap; }
  .filters input, .filters select { width:auto; padding:7px 10px; }.filters input[type=range] { accent-color:var(--accent); }
  #q { width:210px; }#min-score { width:52px; text-align:center; }#min-range { width:130px; }
  .seg { display:inline-flex; border:1px solid var(--border-strong); border-radius:var(--radius); overflow:hidden; }
  .seg button { border:0; background:var(--surface); color:var(--text-dim); font-size:12.5px; padding:6px 10px; cursor:pointer; border-right:1px solid var(--border); }
  .seg button:last-child { border-right:0; }.seg button.on { background:var(--accent); color:var(--accent-text); }
  .count { font-size:12.5px; color:var(--text-dim); margin-left:auto; }.btn-reset { font-size:12px; color:var(--text-dim); background:none; border:0; cursor:pointer; text-decoration:underline; padding:0; }
  .section-heading { display:flex; align-items:end; justify-content:space-between; gap:12px; margin:22px 0 8px; }
  .section-heading h2 { font-size:15px; margin:0; }.section-heading p { color:var(--text-dim); font-size:12.5px; margin:2px 0 0; }
  .row-item { padding:15px 18px; }.row-head { display:flex; align-items:center; gap:9px; flex-wrap:wrap; }
  .badge { font-size:13px; font-weight:600; padding:2px 10px; border-radius:100px; border:1px solid var(--border); flex-shrink:0; }
  .badge.high { background:var(--success-bg); color:var(--success-text); border-color:var(--success-border); }.badge.mid { background:var(--warn-bg); color:var(--warn-text); border-color:var(--warn-border); }.badge.low { background:var(--danger-bg); color:var(--danger-text); border-color:var(--danger-border); }
  .row-title { font-weight:600; font-size:14.5px; }.row-title a { color:var(--text); text-decoration:none; }.row-title a:hover { color:var(--accent); }.row-company { color:var(--text-dim); font-size:14.5px; }
  .chip { font-size:11px; padding:1px 8px; border-radius:100px; border:1px solid var(--border); flex-shrink:0; }.chip.accent { background:var(--accent-soft); color:var(--accent); border-color:var(--accent-border); }.chip.yes { background:var(--success-bg); color:var(--success-text); border-color:var(--success-border); }.chip.no { background:var(--danger-bg); color:var(--danger-text); border-color:var(--danger-border); }.chip.unknown { color:var(--text-faint); }
  .source-tag { font-size:10.5px; text-transform:uppercase; letter-spacing:.03em; color:var(--text-faint); border:1px solid var(--border); border-radius:5px; padding:1px 6px; flex-shrink:0; }
  .meta { font-size:12.5px; color:var(--text-dim); margin:7px 0 0; }.reason { font-size:13px; margin-top:7px; }.score-detail { color:var(--text-dim); font-size:12px; margin-top:5px; }
  .tags { margin-top:7px; display:flex; gap:6px; flex-wrap:wrap; }.tag { font-size:11px; padding:1px 7px; border-radius:5px; }.tag.m { background:var(--success-bg); color:var(--success-text); }.tag.x { background:var(--bg); color:var(--text-dim); border:1px solid var(--border); }
  .card-actions { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:12px; }.card-actions a { font-size:12.5px; color:var(--accent); }
  .status-tools { display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin-top:12px; padding-top:12px; border-top:1px solid var(--border); }
  .status-form { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px 12px; }
  .status-form label { margin:0; font-size:11.5px; color:var(--text-dim); }
  .status-form input, .status-form select, .status-form textarea { margin-top:4px; }
  .status-form .full { grid-column:1 / -1; }
  .status-form .checks { margin:0; }.status-form .checks label { color:var(--text); }
  .status-form-actions { display:flex; align-items:center; gap:8px; grid-column:1 / -1; }
  .submission-fields[hidden] { display:none; }
  .status-modal[hidden] { display:none; }
  .status-modal { position:fixed; inset:0; z-index:50; display:grid; place-items:center; padding:18px;
                  background:color-mix(in srgb, #000 48%, transparent); }
  .status-dialog { width:min(100%,560px); max-height:min(760px,calc(100vh - 36px)); overflow:auto;
                   background:var(--surface); border:1px solid var(--border-strong);
                   border-radius:var(--radius-lg); padding:18px; box-shadow:0 18px 60px #0004; }
  .status-dialog-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:14px; }
  .status-dialog-head h2 { margin:0 0 3px; font-size:16px; }
  .status-dialog-head p { margin:0; color:var(--text-dim); font-size:12px; }
  .modal-close { border:0; background:transparent; color:var(--text-dim); cursor:pointer;
                 padding:2px 7px; font-size:22px; line-height:1; }
  .same-status { grid-column:1 / -1; margin:0; padding:8px 10px; border-radius:var(--radius);
                 color:var(--warn-text); background:var(--warn-bg); font-size:12px; }
  body.modal-open { overflow:hidden; }
  .apply-panel { margin-top:12px; padding-top:12px; border-top:1px solid var(--border); display:flex; align-items:center; justify-content:space-between; gap:14px; flex-wrap:wrap; }
  .apply-copy { min-width:220px; flex:1; }.apply-copy strong { display:block; font-size:12.5px; margin-bottom:3px; }.apply-copy span { color:var(--text-dim); font-size:12px; }
  .apply-form { display:flex; align-items:center; gap:9px; flex-wrap:wrap; margin:0; }.apply-status { color:var(--text-dim); font-size:12px; }
  .apply-status.error { color:var(--danger-text); }.apply-status.success { color:var(--success-text); }
  .apply-status.running::before { content:""; display:inline-block; width:8px; height:8px; margin-right:6px; border:2px solid var(--accent-border); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
  .action-lock { color:var(--text-faint); font-size:12px; }@keyframes spin{to{transform:rotate(360deg)}}
  .empty { color:var(--text-dim); font-size:14px; margin-top:40px; text-align:center; }.no-match { color:var(--text-dim); font-size:14px; margin-top:30px; text-align:center; }
  .load-more { display:flex; justify-content:center; margin:18px 0 8px; }
  .load-more[hidden] { display:none; }
  @media (max-width:850px){.stats{grid-template-columns:repeat(3,1fr)}}
  @media (max-width:600px){.stats{grid-template-columns:repeat(2,1fr)}.today-banner{align-items:flex-start; flex-direction:column}.rules-card{grid-template-columns:1fr}.rule-grid{grid-template-columns:1fr}.filter-row{gap:9px}.filters label{width:100%}#q{width:100%}.count{margin-left:0}.status-tools .btn{flex:1;justify-content:center}.status-form{grid-template-columns:1fr}.status-form .full{grid-column:1}.status-modal{padding:10px}.status-dialog{max-height:calc(100vh - 20px);padding:16px}}
</style></head><body>
<header class="top"><h1>Dashboard</h1><nav class="tabs">
  <a href="/">设置</a><a href="/profile">档案</a><a href="/scoring-rules">评分规则</a><a href="/dashboard" class="active">Dashboard</a>
</nav></header>
<p class="sub">结果、匹配理由、每日行动和投递状态统一在这里。工作机会始终按匹配分从高到低排列。</p>
{% if saved %}<div class="flash">状态已更新，并已写入操作事件。</div>{% endif %}
{% if error %}<div class="card"><h2>无法更新状态</h2><p class="hint">{{ error }}</p></div>{% endif %}
<div class="stats">
  <div class="stat"><div class="n">{{ stats.total }}</div><div class="k">全部机会</div></div>
  <div class="stat high"><div class="n">{{ stats.within_24h }}</div><div class="k">24h 内发布</div></div>
  <div class="stat"><div class="n">{{ stats.ready_to_apply }}</div><div class="k">待投递</div></div>
  <div class="stat high"><div class="n">{{ stats.submitted }}</div><div class="k">已提交</div></div>
  <div class="stat danger"><div class="n">{{ stats.rejected }}</div><div class="k">被拒绝</div></div>
  <div class="stat mid"><div class="n">{{ stats.interview }}</div><div class="k">面试中</div></div>
</div>

<div class="today-banner">
  <div><strong>投递清单 · 点击后只记录本地任务</strong><span>按钮只会把岗位加入本地投递清单，不启动后台进程、不打开招聘网站。当前 Agent 通过 --handoff-list 读取并按 applypilot-au 规则执行。</span></div>
  <div class="status-tools">
    <span class="hint">最近同步 {{ latest_sync_count }} 条 · {{ latest_sync_display }}{% if latest_run_id %} · 批次 {{ latest_run_id }}{% endif %}</span>
    <button type="button" class="btn" onclick="setQueue('latest', document.querySelector('[data-v=latest]'))">查看最近同步</button>
    <button type="button" class="btn" onclick="setSeg('freshness', 'within_24h', document.querySelector('[data-v=within_24h]'))">只看 24h</button>
  </div>
</div>

<div class="card rules-card">
  <div><h2>DeepSeek 打分规则</h2><p class="hint">当前规则 sha256: {{ scoring_rules_hash }}。完整规则由你审核和修改，Agent 不会自行改写。</p><a class="btn" href="/scoring-rules">查看并编辑完整规则 →</a></div>
  <div class="rule-grid">{% for item in rule_dimensions %}<div class="rule-dimension"><span>{{ item.name }}</span><b>{{ item.points }} 分</b></div>{% endfor %}</div>
</div>

{% if not rows %}
<p class="empty">还没有 Dashboard 记录 —— 先在设置面板运行“抓取 + 打分”。</p>
{% else %}
<div class="filters">
  <div class="filter-row">
    <label>最低分 <input type="range" id="min-range" min="-1" max="100" step="1" value="-1" oninput="syncRange()"><input type="number" id="min-score" min="-1" max="100" value="-1" oninput="syncNumber()"></label>
    <label>搜索 <input type="text" id="q" placeholder="公司 / 职位 / 匹配理由" oninput="filterRows(true)"></label>
  </div>
  <div class="filter-row">
    <label>视图 <span class="seg"><button class="on" data-v="all" onclick="setQueue('all', this)">全部</button><button data-v="latest" onclick="setQueue('latest', this)">最近同步</button><button data-v="today" onclick="setQueue('today', this)">今日行动</button><button data-v="history" onclick="setQueue('history', this)">历史</button></span></label>
    <label>状态 <span class="seg"><button class="on" data-v="all" onclick="setSeg('status', 'all', this)">全部</button><button data-v="ready_to_apply" onclick="setSeg('status', 'ready_to_apply', this)">待投递</button><button data-v="submitted" onclick="setSeg('status', 'submitted', this)">已提交</button><button data-v="rejected" onclick="setSeg('status', 'rejected', this)">被拒绝</button><button data-v="interview" onclick="setSeg('status', 'interview', this)">面试中</button></span></label>
  </div>
  <div class="filter-row">
    <label>模式 <span class="seg"><button class="on" data-v="all" onclick="setSeg('mode', 'all', this)">全部</button><button data-v="targeted" onclick="setSeg('mode', 'targeted', this)">精准</button><button data-v="broad" onclick="setSeg('mode', 'broad', this)">海投</button><button data-v="manual_review" onclick="setSeg('mode', 'manual_review', this)">人工复核</button></span></label>
    <label>担保 <span class="seg"><button class="on" data-v="all" onclick="setSeg('sponsor', 'all', this)">全部</button><button data-v="explicit_yes" onclick="setSeg('sponsor', 'explicit_yes', this)">可担保</button><button data-v="unknown" onclick="setSeg('sponsor', 'unknown', this)">未提及</button><button data-v="explicit_no" onclick="setSeg('sponsor', 'explicit_no', this)">不担保</button></span></label>
    <span class="count" id="count"></span>
  </div>
  <div class="filter-row">
    <label>来源 <span class="seg"><button class="on" data-v="all" onclick="setSeg('source', 'all', this)">全部</button>{% for s in sources %}<button data-v="{{ s }}" onclick="setSeg('source', '{{ s }}', this)">{{ s }}</button>{% endfor %}</span></label>
    <label>新鲜度 <span class="seg"><button class="on" data-v="all" onclick="setSeg('freshness', 'all', this)">全部</button><button data-v="within_24h" onclick="setSeg('freshness', 'within_24h', this)">24 小时</button><button data-v="within_3d" onclick="setSeg('freshness', 'within_3d', this)">3 天</button></span></label>
    <button class="btn-reset" onclick="resetFilters()">重置筛选</button>
  </div>
</div>

<div class="section-heading"><div><h2>工作机会</h2><p>按匹配分降序排列；状态和历史保留在同一张卡片里。</p></div><span class="count" id="count-secondary"></span></div>
<div id="rows">
{% for r in rows %}
{% set sig = r.sponsorship_signal or 'unknown' %}
<div class="card row-item" data-score="{{ r.score_int }}" data-text="{{ (r.title ~ ' ' ~ r.company ~ ' ' ~ r.summary ~ ' ' ~ r.reason ~ ' ' ~ r.matched ~ ' ' ~ r.missing)|lower }}" data-status="{{ r.display_status }}" data-mode="{{ r.application_mode or 'manual_review' }}" data-sponsor="{{ sig }}" data-source="{{ r.source }}" data-freshness="{{ r.freshness_bucket or 'unknown' }}" data-today="{{ r.today_action }}" data-latest="{{ r.latest_run }}" data-posted-at="{{ r.posted_at_raw }}">
  <div class="row-head">
    <span class="badge {{ r.score_class }}">{{ r.score_label }}</span>
    <span class="row-title"><a href="{{ url_for('dashboard_detail', job_id=r.job_id) }}">{{ r.title }}</a></span>
    <span class="row-company">@ {{ r.company }}</span>
    <span class="chip accent">{{ r.status_label }}</span>
    <span class="chip">{{ r.mode_label }}</span>
    <span class="chip" data-freshness-chip="1">{{ r.freshness_label }}</span>
    {% if sig == 'explicit_yes' %}<span class="chip yes">可担保</span>{% elif sig == 'explicit_no' %}<span class="chip no">不担保</span>{% else %}<span class="chip unknown">担保未提及</span>{% endif %}
    <span class="source-tag">{{ r.source_label }}</span>
  </div>
  <div class="meta">{{ r.location or '地点未知' }} · 发布于 {{ r.posted_display }}{% if r.posted_relative %}（<span data-posted-relative="1">{{ r.posted_relative }}</span>）{% endif %} · {{ r.salary_raw or '薪资未列出' }} · {{ r.resume_id or '未选择简历' }}</div>
  <div class="reason"><b>JD 摘要：</b>{{ r.summary or 'DeepSeek 暂未返回摘要。' }}</div>
  <div class="score-detail"><b>评分理由：</b>{{ r.reason or '暂无匹配理由，需人工复核。' }}</div>
  <div class="score-detail">简历匹配：{{ r.resume_fit_score or '—' }}/100{% if r.resume_reason %} · {{ r.resume_reason }}{% endif %}{% if r.eligibility_reason %} · {{ r.eligibility_reason }}{% endif %}</div>
  <div class="tags">{% for m in (r.matched or '').split(',') if m.strip() %}<span class="tag m">{{ m.strip() }}</span>{% endfor %}{% for x in (r.missing or '').split(',') if x.strip() %}<span class="tag x">缺 {{ x.strip() }}</span>{% endfor %}</div>
  <div class="card-actions"><a href="{{ r.url }}" target="_blank" rel="noopener">打开岗位链接 →</a><a href="{{ url_for('dashboard_detail', job_id=r.job_id) }}">查看详情与历史 →</a>{% if r.next_action %}<span class="meta">下一步：{{ r.next_action }}</span>{% endif %}</div>
  <div class="status-tools">
    {% if r.display_status == 'ready_to_apply' %}
    <button type="button" class="btn primary status-action"
      data-action="{{ url_for('dashboard_transition', job_id=r.job_id) }}"
      data-title="{{ r.title }} @ {{ r.company }}"
      data-current="{{ r.display_status }}"
      data-evidence="{{ r.submission_evidence }}"
      onclick="openStatusDialog(this,'confirm')">确认已提交</button>
    {% endif %}
    <button type="button" class="btn status-action"
      data-action="{{ url_for('dashboard_transition', job_id=r.job_id) }}"
      data-title="{{ r.title }} @ {{ r.company }}"
      data-current="{{ r.display_status }}"
      data-evidence="{{ r.submission_evidence }}"
      onclick="openStatusDialog(this,'change')">转换申请状态</button>
  </div>
  {% if r.application_mode == 'manual_review' %}
  <div class="apply-panel"><span class="action-lock">当前规则要求人工复核职位级别或资格后才能投递。</span></div>
  {% elif r.application_mode in ['broad', 'targeted'] %}
  <div class="apply-panel" id="apply-panel-{{ r.job_id }}">
    <div class="apply-copy"><strong>{{ r.platform_mode_label }}</strong><span>{{ r.apply_explanation }} 只有成功确认页才会记为“已提交”。</span></div>
    <div class="apply-form">
      <span class="apply-status" id="apply-status-{{ r.job_id }}">{% if r.attempt_status_label %}{{ r.attempt_status_label }}{% else %}尚未加入清单{% endif %}</span>
      {% if r.display_status == 'ready_to_apply' and r.attempt_status not in ['submitted', 'selected', 'queued', 'browser_opened', 'filling', 'ready_to_submit'] %}
      <form method="post" action="{{ url_for('queue_applypilot', job_id=r.job_id) }}" onsubmit="return addToApplicationList(event, this)">
        <input type="hidden" name="action_token" value="{{ action_token }}">
        <button class="btn primary" type="submit">{{ r.apply_button_label }}</button>
      </form>
      {% endif %}
      {% if r.attempt_status == 'needs_user' %}<a href="{{ url_for('dashboard_detail', job_id=r.job_id) }}">查看需要处理的事项 →</a>{% endif %}
    </div>
  </div>
  {% endif %}
</div>
{% endfor %}
</div>
<p class="no-match" id="no-match" style="display:none">没有符合筛选条件的岗位。</p>
<div class="load-more" id="load-more" hidden>
  <button type="button" class="btn" onclick="loadMore()">加载更多岗位</button>
</div>

<div class="status-modal" id="status-modal" role="dialog" aria-modal="true"
  aria-labelledby="status-dialog-title" hidden onclick="if(event.target===this)closeStatusDialog()">
  <div class="status-dialog">
    <div class="status-dialog-head">
      <div><h2 id="status-dialog-title">转换申请状态</h2><p id="status-dialog-job"></p></div>
      <button type="button" class="modal-close" aria-label="关闭" onclick="closeStatusDialog()">×</button>
    </div>
    <p class="hint" id="status-dialog-hint">每次状态变化都会写入历史记录。</p>
    <form class="status-form" id="status-form" method="post">
      <input type="hidden" name="action_token" value="{{ action_token }}">
      <input type="hidden" name="return_to" value="dashboard">
      <label id="status-choice">新状态
        <select class="status-select" id="status-select" name="status" onchange="updateStatusDialog()">
          {% for status in statuses %}<option value="{{ status }}">{{ status_labels[status] }}</option>{% endfor %}
        </select>
      </label>
      <label>原因或备注
        <input type="text" id="status-reason" name="reason" placeholder="例如：收到面试邀请">
      </label>
      <p class="same-status" id="same-status" hidden>请选择与当前状态不同的新状态。</p>
      <div class="submission-fields full" id="submission-fields" hidden>
        <label>提交成功证据
          <input type="text" id="submission-evidence" name="submission_evidence"
            placeholder="成功页文字、确认编号或确认 URL">
        </label>
        <div class="checks"><label><input type="checkbox" id="submission-confirmed"
          name="submission_confirmed">我确认：此岗位已在外部平台成功提交</label></div>
      </div>
      <div class="status-form-actions">
        <button class="btn primary" id="status-save" type="submit">保存新状态</button>
        <button class="btn" type="button" onclick="closeStatusDialog()">取消</button>
      </div>
    </form>
  </div>
</div>

<script>
const state = { queue: 'all', status: 'all', mode: 'all', sponsor: 'all', source: 'all', freshness: 'all' };
const freshnessPriorityHours = {{ freshness_priority_hours }};
const freshnessRecentHours = {{ freshness_recent_hours }};
const freshnessLabels = {within_24h:'24 小时内', within_3d:'3 天内', older:'较早发布', unknown:'发布时间未知'};
const pageSize = 40;
let visibleLimit = pageSize;
let statusTrigger = null;
function syncRange(){document.getElementById('min-score').value=document.getElementById('min-range').value;filterRows(true);}
function syncNumber(){let v=parseInt(document.getElementById('min-score').value||'-1',10);v=Math.max(-1,Math.min(100,isNaN(v)?-1:v));document.getElementById('min-range').value=v;filterRows(true);}
function setQueue(value, btn){state.queue=value;document.querySelectorAll('.filters .seg').forEach(seg=>{if(seg.querySelector('[onclick^="setQueue"]'))seg.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.v===value));});if(btn&&btn.parentNode&&btn.parentNode.classList.contains('seg'))btn.parentNode.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.v===value));filterRows(true);}
function setSeg(group,value,btn){if(!btn)return;state[group]=value;btn.parentNode.querySelectorAll('button').forEach(b=>b.classList.remove('on'));btn.classList.add('on');filterRows(true);}
function resetFilters(){document.getElementById('min-score').value=-1;document.getElementById('min-range').value=-1;document.getElementById('q').value='';state.queue='all';state.status='all';state.mode='all';state.sponsor='all';state.source='all';state.freshness='all';document.querySelectorAll('.seg').forEach(seg=>seg.querySelectorAll('button').forEach(b=>b.classList.toggle('on',b.dataset.v==='all')));filterRows(true);}
function filterRows(resetLimit=false){
  if(resetLimit)visibleLimit=pageSize;
  const min=parseInt(document.getElementById('min-score').value||'-1',10);
  const q=document.getElementById('q').value.toLowerCase().trim();
  const matches=[];
  document.querySelectorAll('.row-item').forEach(el=>{
    const score=parseInt(el.dataset.score,10);
    const queueOk=state.queue==='all'||(state.queue==='latest'&&el.dataset.latest==='1')||(state.queue==='today'&&el.dataset.today==='1')||(state.queue==='history'&&el.dataset.today!=='1');
    const ok=score>=min&&queueOk&&el.dataset.text.includes(q)&&(state.status==='all'||el.dataset.status===state.status)&&(state.mode==='all'||el.dataset.mode===state.mode)&&(state.sponsor==='all'||el.dataset.sponsor===state.sponsor)&&(state.source==='all'||el.dataset.source===state.source)&&(state.freshness==='all'||el.dataset.freshness===state.freshness);
    el.style.display='none';
    if(ok)matches.push(el);
  });
  const shown=Math.min(visibleLimit,matches.length);
  matches.slice(0,shown).forEach(el=>{el.style.display='';});
  document.getElementById('count').textContent='显示 '+shown+' / '+matches.length+' 条匹配（共 {{ rows|length }} 条）';
  document.getElementById('count-secondary').textContent=matches.length+' 条匹配';
  document.getElementById('no-match').style.display=matches.length===0?'':'none';
  const loadMore=document.getElementById('load-more');
  loadMore.hidden=shown>=matches.length;
  if(!loadMore.hidden)loadMore.querySelector('button').textContent='再显示 '+Math.min(pageSize,matches.length-shown)+' 条';
}
function loadMore(){visibleLimit+=pageSize;filterRows(false);}
function postedTimestamp(raw){
  if(!raw)return null;
  const timestamp=Date.parse(raw.trim().replace(/Z$/,'+00:00'));
  return Number.isFinite(timestamp)?timestamp:null;
}
function currentFreshness(raw){
  const timestamp=postedTimestamp(raw);
  if(timestamp===null)return 'unknown';
  const ageHours=Math.max(0,(Date.now()-timestamp)/3600000);
  if(ageHours<=freshnessPriorityHours)return 'within_24h';
  if(ageHours<=freshnessRecentHours)return 'within_3d';
  return 'older';
}
function currentRelativeAge(raw){
  const timestamp=postedTimestamp(raw);
  if(timestamp===null)return '';
  const ageHours=Math.max(0,Math.floor((Date.now()-timestamp)/3600000));
  return ageHours<48?(ageHours===0?'刚刚发布':ageHours+' 小时前'):Math.floor(ageHours/24)+' 天前';
}
function refreshFreshness(){
  document.querySelectorAll('.row-item').forEach(el=>{
    const bucket=currentFreshness(el.dataset.postedAt||'');
    el.dataset.freshness=bucket;
    const chip=el.querySelector('[data-freshness-chip]');
    if(chip)chip.textContent=freshnessLabels[bucket];
    const relative=el.querySelector('[data-posted-relative]');
    if(relative){const text=currentRelativeAge(el.dataset.postedAt||'');if(text)relative.textContent=text;}
  });
  filterRows();
}
function openStatusDialog(button,mode){
  statusTrigger=button;
  const modal=document.getElementById('status-modal');
  const form=document.getElementById('status-form');
  form.reset();
  form.action=button.dataset.action;
  modal.dataset.current=button.dataset.current;
  modal.dataset.mode=mode;
  document.getElementById('status-dialog-job').textContent=button.dataset.title;
  document.getElementById('submission-evidence').value=button.dataset.evidence||'';
  const select=document.getElementById('status-select');
  select.value=mode==='confirm'?'submitted':button.dataset.current;
  document.getElementById('status-choice').hidden=mode==='confirm';
  document.getElementById('status-dialog-title').textContent=mode==='confirm'?'确认已提交':'转换申请状态';
  document.getElementById('status-dialog-hint').textContent=mode==='confirm'
    ?'仅在招聘平台已显示成功页、确认编号或确认信息后使用。'
    :'可在待投递、已提交、被拒绝和面试中之间转换；变化会写入历史记录。';
  document.getElementById('status-save').textContent=mode==='confirm'?'确认并记录':'保存新状态';
  updateStatusDialog();
  modal.hidden=false;
  document.body.classList.add('modal-open');
  requestAnimationFrame(()=>{(mode==='confirm'?document.getElementById('submission-evidence'):select).focus();});
}
function closeStatusDialog(){
  const modal=document.getElementById('status-modal');
  modal.hidden=true;
  document.body.classList.remove('modal-open');
  if(statusTrigger)statusTrigger.focus();
}
function updateStatusDialog(){
  const modal=document.getElementById('status-modal');
  const select=document.getElementById('status-select');
  const submitted=select.value==='submitted';
  const fields=document.getElementById('submission-fields');
  const evidence=document.getElementById('submission-evidence');
  const confirmed=document.getElementById('submission-confirmed');
  fields.hidden=!submitted;
  evidence.required=submitted;
  confirmed.required=submitted;
  const same=modal.dataset.mode==='change'&&select.value===modal.dataset.current;
  document.getElementById('same-status').hidden=!same;
  const reason=document.getElementById('status-reason');
  const returning=select.value==='ready_to_apply'&&['submitted','rejected','interview'].includes(modal.dataset.current);
  reason.required=returning;
  reason.placeholder=returning?'请说明为什么重新进入待投递':'例如：收到面试邀请';
  document.getElementById('status-save').disabled=same;
}
async function addToApplicationList(event, form){
  event.preventDefault();
  const button=form.querySelector('button');
  const panel=form.closest('.apply-panel');
  const status=panel.querySelector('.apply-status');
  button.disabled=true;
  status.className='apply-status';
  status.textContent='正在加入投递清单…';
  try{
    const response=await fetch(form.action,{method:'POST',body:new FormData(form),headers:{'Accept':'application/json'}});
    const data=await response.json();
    if(!response.ok||!data.ok)throw new Error(data.error||'无法加入投递清单');
    status.textContent=data.message||data.attempt_status_label||'已加入投递清单';
    button.textContent='已加入投递清单';
  }catch(error){
    status.className='apply-status error';
    status.textContent=error.message;
    button.disabled=false;
  }
  return false;
}
document.addEventListener('keydown',event=>{
  if(event.key==='Escape'&&!document.getElementById('status-modal').hidden)closeStatusDialog();
});
const transientUrl=new URL(window.location.href);
let transientChanged=false;
['saved','error'].forEach(key=>{
  if(transientUrl.searchParams.has(key)){transientUrl.searchParams.delete(key);transientChanged=true;}
});
if(transientChanged)history.replaceState(null,'',transientUrl.pathname+transientUrl.search);
filterRows(true);
let dashboardRunSeen = false;
const initialDashboardSyncMarker = {{ (latest_run_id ~ '|' ~ latest_sync_display ~ '|' ~ latest_sync_count)|tojson }};
async function watchDashboardRun(){
  try{
    const [runResponse,syncResponse]=await Promise.all([
      fetch('/run/status',{cache:'no-store'}),
      fetch('/dashboard/status',{cache:'no-store'}),
    ]);
    const data=await runResponse.json();
    const sync=await syncResponse.json();
    if(data.running)dashboardRunSeen=true;
    if(sync.marker&&sync.marker!==initialDashboardSyncMarker){window.location.reload();return;}
    if(!data.running&&dashboardRunSeen){window.location.reload();return;}
  }catch(error){/* the next poll will retry while the local app is available */}
  setTimeout(watchDashboardRun,dashboardRunSeen?1500:5000);
}
watchDashboardRun();
setInterval(refreshFreshness,60000);
</script>
{% endif %}
</body></html>
"""

DASHBOARD_DETAIL_HTML = """
<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{{ row.title }} · Dashboard</title><style>""" + BASE_STYLE + """</style></head><body>
<header class="top"><h1>岗位详情</h1><nav class="tabs"><a href="/dashboard" class="active">← Dashboard</a><a href="/profile">档案</a><a href="/scoring-rules">评分规则</a></nav></header>
<div class="card"><h2>{{ row.title }} @ {{ row.company }}</h2><p class="hint">{{ row.location or '地点未知' }} · 发布于 {{ row.posted_display }}{% if row.posted_relative %}（{{ row.posted_relative }}）{% endif %} · {{ row.freshness_label }} · <a href="{{ row.url }}" target="_blank" rel="noopener">打开原始岗位链接</a></p>
<p><b>JD 摘要：</b>{{ row.job_summary or 'DeepSeek 暂未返回摘要。' }}<br><b>岗位匹配：</b>{{ row.job_fit_score or '—' }}/100 — {{ row.job_fit_reason or '—' }}<br><b>简历：</b>{{ row.resume_id or '—' }} · {{ row.resume_fit_score or '—' }}/100 — {{ row.resume_reason or '—' }}<br><b>申请路径：</b>{{ row.application_mode or 'manual_review' }} — {{ row.eligibility_reason or '—' }}</p>
{% if row.artifact_path %}<p class="hint">材料目录：{{ row.artifact_path }}</p>{% endif %}</div>
<div class="card"><h2>更新记录</h2><p class="hint">Dashboard 是展示账本。只有平台成功页或确认文本可把岗位标记为 submitted；Agent 选择、打开页面或填完表单都不算提交。</p>
<form method="post" action="{{ url_for('dashboard_transition', job_id=row.job_id) }}">
<input type="hidden" name="action_token" value="{{ action_token }}">
<label>状态</label><select name="status">{% for status in statuses %}<option value="{{ status }}" {% if status == row.display_status %}selected{% endif %}>{{ status_labels[status] }}</option>{% endfor %}</select>
<label>原因或备注</label><textarea name="reason" rows="2"></textarea>
<label>下一步动作</label><input type="text" name="next_action" value="{{ row.next_action }}">
<label>备注</label><textarea name="notes" rows="3">{{ row.notes }}</textarea>
<label>提交证据（submitted 时必填）</label><input type="text" name="submission_evidence" value="{{ row.submission_evidence }}" placeholder="例如：成功页文本或确认 URL">
<div class="checks"><label><input type="checkbox" name="submission_confirmed">我确认：此岗位已在外部平台成功提交</label></div>
<p><button class="btn primary" type="submit">保存状态更新</button></p>
</form></div>
{% if row.application_mode in ['broad', 'targeted'] %}<div class="card"><h2>ApplyPilot 投递</h2>
{% if attempt %}<p><span class="pill">{{ attempt.status_label }}</span> {{ attempt.platform_mode_label }}</p><p class="hint">Dashboard 卡片上的投递按钮会启动或继续此记录；只有明确的外部成功证据才算已提交。</p>
{% else %}<p class="hint">请返回 Dashboard，在该岗位卡片点击投递按钮。当前方式：{{ platform_mode_label }}。</p>{% endif %}
</div>{% endif %}
<div class="card"><h2>操作历史</h2>{% for event in events %}<div class="job"><b>{{ event.action }}</b> · {{ event.timestamp }}<div class="meta">{{ event.from_status or '—' }} → {{ event.to_status or '—' }} · {{ event.actor }}{% if event.reason %} · {{ event.reason }}{% endif %}</div></div>{% else %}<p class="hint">暂无事件。</p>{% endfor %}</div>
</body></html>
"""

@app.route("/", methods=["GET"])
def index():
    cfg = load_config()
    terms = "\n".join(cfg["search"]["terms"])
    exclude_keywords = "\n".join(cfg["visa"]["exclude_keywords"])
    bonus_keywords = "\n".join(cfg["visa"]["bonus_keywords"])
    preferences = PREFS_PATH.read_text(encoding="utf-8") if PREFS_PATH.exists() else ""
    with RUN_LOCK:
        log_text = "\n".join(RUN_STATE["log"]) or "(还没跑过)"
        run_state = dict(RUN_STATE)
    return render_template_string(
        INDEX_HTML, cfg=cfg, terms=terms, exclude_keywords=exclude_keywords,
        bonus_keywords=bonus_keywords, preferences=preferences,
        saved=request.args.get("saved") == "1",
        log_text=log_text, run_state=run_state,
    )


@app.route("/profile", methods=["GET"])
def candidate_profile():
    cfg = load_config()
    _, profile = _candidate_profile(cfg)
    return render_template_string(
        PROFILE_HTML, sections=profile_form_sections(profile),
        missing=profile.validation_items(), saved=request.args.get("saved") == "1",
    )


@app.route("/profile/save", methods=["POST"])
def candidate_profile_save():
    cfg = load_config()
    path, profile = _candidate_profile(cfg)
    save_profile(path, update_from_form(profile, request.form))
    return redirect(url_for("candidate_profile", saved="1"))


@app.route("/scoring-rules", methods=["GET"])
def scoring_rules_view():
    cfg = load_config()
    try:
        path, content, rules_hash = _load_scoring_rules(cfg)
    except (OSError, ValueError) as exc:
        return render_template_string(
            SCORING_RULES_HTML, rules_path="", content="", rules_hash="unavailable",
            saved=False, error=str(exc),
        ), 500
    return render_template_string(
        SCORING_RULES_HTML, rules_path=path, content=content, rules_hash=rules_hash,
        saved=request.args.get("saved") == "1", error=request.args.get("error", ""),
    )


@app.route("/scoring-rules/save", methods=["POST"])
def scoring_rules_save():
    cfg = load_config()
    try:
        _save_scoring_rules(
            cfg,
            request.form.get("content", ""),
            request.form.get("expected_hash", ""),
        )
    except (OSError, ValueError) as exc:
        return redirect(url_for("scoring_rules_view", error=str(exc)))
    return redirect(url_for("scoring_rules_view", saved="1"))


@app.route("/dashboard", methods=["GET"])
def dashboard_view():
    cfg = load_config()
    board = _dashboard(cfg)
    raw_rows = board.load_rows()
    queues = board.daily_queues()
    timezone_name = str(
        cfg.get("applypilot", {}).get("timezone", "Australia/Melbourne")
    )
    latest_run_id, latest_sync_display, latest_sync_count = _latest_sync_info(
        raw_rows, timezone_name,
    )
    today_ids = {row.get("job_id", "") for row in queues["today"]}
    attempts_by_job: dict[str, dict[str, str]] = {}
    for attempt in _attempts(cfg).load_rows():
        job_id = attempt.get("job_id", "")
        if not job_id or attempt.get("status") in {"cancelled", "failed"}:
            continue
        previous = attempts_by_job.get(job_id)
        if previous is None or attempt.get("updated_at", "") >= previous.get("updated_at", ""):
            attempts_by_job[job_id] = attempt
    ranked_path = _project_path(cfg.get("paths", {}).get("output_dir", "output")) / "jobs-ranked.csv"
    rows = _unified_dashboard_rows(
        raw_rows, today_ids, attempts_by_job,
        timezone_name=timezone_name,
        freshness_config=cfg,
        latest_run_id=latest_run_id,
        ranked_path=ranked_path,
    )
    try:
        _, scoring_rules, scoring_rules_hash = _load_scoring_rules(cfg)
        rule_dimensions = _extract_score_dimensions(scoring_rules)
    except (OSError, ValueError):
        scoring_rules_hash = "unavailable"
        rule_dimensions = []
    stats = {
        "total": len(rows),
        "within_24h": sum(
            1 for row in rows if row.get("freshness_bucket") == "within_24h"
        ),
        "ready_to_apply": sum(
            1 for row in rows if row.get("display_status") == "ready_to_apply"
        ),
        "submitted": sum(
            1 for row in rows if row.get("display_status") == "submitted"
        ),
        "rejected": sum(
            1 for row in rows if row.get("display_status") == "rejected"
        ),
        "interview": sum(
            1 for row in rows if row.get("display_status") == "interview"
        ),
    }
    sources = sorted({row.get("source", "") for row in rows if row.get("source")})
    response = make_response(render_template_string(
        DASHBOARD_HTML, rows=rows, queues=queues, stats=stats, sources=sources,
        scoring_rules_hash=scoring_rules_hash, rule_dimensions=rule_dimensions,
        action_token=ACTION_TOKEN, statuses=USER_STATUSES,
        status_labels=USER_STATUS_LABELS,
        latest_run_id=latest_run_id, latest_sync_display=latest_sync_display,
        latest_sync_count=latest_sync_count,
        freshness_priority_hours=int(
            cfg.get("application", {}).get("priority_within_hours", 24)
        ),
        freshness_recent_hours=int(
            cfg.get("application", {}).get("recent_within_hours", 72)
        ),
        saved=request.args.get("saved") == "1", error=request.args.get("error", ""),
    ))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/dashboard/status", methods=["GET"])
def dashboard_sync_status():
    cfg = load_config()
    rows = _dashboard(cfg).load_rows()
    timezone_name = str(
        cfg.get("applypilot", {}).get("timezone", "Australia/Melbourne")
    )
    latest_run_id, latest_sync_display, latest_sync_count = _latest_sync_info(
        rows, timezone_name,
    )
    response = jsonify({
        "marker": f"{latest_run_id}|{latest_sync_display}|{latest_sync_count}",
        "run_id": latest_run_id,
        "synced_count": latest_sync_count,
        "synced_at": latest_sync_display,
    })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.route("/dashboard/<job_id>", methods=["GET"])
def dashboard_detail(job_id: str):
    cfg = load_config()
    board = _dashboard(cfg)
    row = board.get(job_id)
    if row is None:
        return "找不到该 Dashboard 记录", 404
    row = dict(row)
    if (row.get("source") or "").strip().lower() == "seek":
        row["title"] = clean_seek_title(row.get("title", ""))
    ranked_path = _project_path(cfg.get("paths", {}).get("output_dir", "output")) / "jobs-ranked.csv"
    ranked = _ranked_lookup(ranked_path)
    ranked_row = ranked.get(("url", (row.get("url") or "").strip()), {})
    row["job_summary"] = row.get("job_summary") or ranked_row.get("summary", "")
    row["display_status"] = _user_status(row.get("status", ""))
    row["freshness_bucket"] = board.current_freshness(row)
    row["freshness_label"] = FRESHNESS_LABELS.get(
        row["freshness_bucket"], row["freshness_bucket"]
    )
    row["posted_display"], row["posted_relative"] = _format_posted_at(
        row.get("posted_at", ""),
        str(cfg.get("applypilot", {}).get("timezone", "Australia/Melbourne")),
    )
    service = _attempts(cfg)
    attempts = [
        attempt for attempt in service.load_rows()
        if attempt.get("job_id") == job_id and attempt.get("status") not in {"cancelled", "failed"}
    ]
    attempt = max(attempts, key=lambda item: item.get("updated_at", ""), default=None)
    if attempt:
        attempt = dict(attempt)
        attempt["status_label"] = ATTEMPT_LABELS.get(attempt.get("status", ""), attempt.get("status", ""))
        mode = platform_submission_mode(row.get("source", ""), row.get("url", ""))
        attempt["platform_mode_label"] = PLATFORM_MODE_LABELS.get(mode, mode)
    platform_mode = platform_submission_mode(row.get("source", ""), row.get("url", ""))
    return render_template_string(
        DASHBOARD_DETAIL_HTML, row=row, events=board.events_for(job_id),
        statuses=USER_STATUSES, status_labels=USER_STATUS_LABELS, attempt=attempt,
        action_token=ACTION_TOKEN,
        platform_mode_label=PLATFORM_MODE_LABELS.get(platform_mode, platform_mode),
    )


@app.route("/dashboard/<job_id>/transition", methods=["POST"])
def dashboard_transition(job_id: str):
    supplied_token = request.form.get("action_token", "")
    if not supplied_token or not hmac.compare_digest(supplied_token, ACTION_TOKEN):
        return "状态请求已失效，请刷新 Dashboard 后重试。", 403

    cfg = load_config()
    board = _dashboard(cfg)
    return_to_dashboard = request.form.get("return_to") == "dashboard"
    try:
        row = board.get(job_id)
        if row is None:
            raise KeyError(f"Dashboard 中找不到岗位: {job_id}")

        status = request.form.get("status", "")
        reason = request.form.get("reason", "").strip()
        evidence = request.form.get("submission_evidence", "").strip()
        submission_confirmed = "submission_confirmed" in request.form
        next_action = request.form["next_action"] if "next_action" in request.form else None
        notes = request.form["notes"] if "notes" in request.form else None

        if status == row.get("status") and next_action is None and notes is None:
            raise ValueError("申请状态没有变化，请选择新的状态")

        current_display_status = _user_status(row.get("status", ""))
        if (
            status == "ready_to_apply"
            and current_display_status in {"submitted", "rejected", "interview"}
            and not reason
        ):
            raise ValueError("将已提交、被拒绝或面试中的岗位改回待投递时，必须填写原因")

        default_next_actions = {
            "ready_to_apply": "继续或重新开始投递",
            "submitted": "等待雇主回复并按计划跟进",
            "rejected": "记录拒信；必要时复盘简历和匹配规则",
            "interview": "准备面试并记录安排",
        }
        if next_action is None:
            next_action = default_next_actions.get(status)

        if status == "submitted":
            submit_reason = reason or "用户确认外部平台已成功提交"
            service = _attempts(cfg)
            attempts = [
                attempt for attempt in service.load_rows()
                if attempt.get("job_id") == job_id
                and attempt.get("status") not in {"cancelled", "failed"}
            ]
            latest_attempt = max(
                attempts, key=lambda item: item.get("updated_at", ""), default=None,
            )
            if latest_attempt and latest_attempt.get("status") != "submitted":
                service.advance(
                    latest_attempt["attempt_id"], "submitted", actor="user",
                    reason=submit_reason, submission_confirmed=submission_confirmed,
                    submission_evidence=evidence, next_action=next_action, notes=notes,
                )
            else:
                board.transition(
                    job_id, status, actor="user", reason=submit_reason,
                    next_action=next_action, notes=notes,
                    submission_confirmed=submission_confirmed,
                    submission_evidence=evidence,
                )
        else:
            board.transition(
                job_id, status, actor="user", reason=reason,
                next_action=next_action, notes=notes,
            )
    except (KeyError, ValueError) as exc:
        return redirect(url_for("dashboard_view", error=str(exc)))
    if return_to_dashboard:
        return redirect(url_for("dashboard_view", saved="1"))
    return redirect(url_for("dashboard_detail", job_id=job_id))


@app.route("/dashboard/<job_id>/applypilot", methods=["POST"])
@app.route("/dashboard/<job_id>/attempt", methods=["POST"])
def queue_applypilot(job_id: str):
    wants_json = "application/json" in request.headers.get("Accept", "")

    def failure(message: str, status_code: int = 400):
        if wants_json:
            return jsonify({"ok": False, "error": message}), status_code
        return redirect(url_for("dashboard_view", error=message))

    supplied_token = request.form.get("action_token", "")
    if not supplied_token or not hmac.compare_digest(supplied_token, ACTION_TOKEN):
        return failure("投递请求已失效，请刷新 Dashboard 后重试。", 403)

    cfg = load_config()
    board = _dashboard(cfg)
    service = _attempts(cfg)
    attempt: dict[str, str] | None = None
    try:
        from run import submitted_today

        apply_cfg = cfg.get("applypilot", {})
        timezone_name = str(apply_cfg.get("timezone", "Australia/Melbourne"))
        hard_cap = int(apply_cfg.get("hard_cap_per_day", 40))
        confirmed_today = submitted_today(board.load_rows(), timezone_name)
        if confirmed_today >= hard_cap:
            raise ValueError(
                f"今天已经确认提交 {confirmed_today} 份，达到每日硬上限 {hard_cap}。"
            )

        profile_path, profile = _candidate_profile(cfg)
        attempt = service.create(job_id, profile, profile_path, actor="user")
        if attempt.get("readiness") != "ready":
            reason = attempt.get("reason") or "Candidate Profile 尚未达到真实投递要求。"
            if attempt.get("status") != "needs_user":
                attempt = service.advance(
                    attempt["attempt_id"], "needs_user", actor="system", reason=reason,
                )
            payload = _attempt_public_payload(
                attempt, message=f"需要先补全 Candidate Profile：{reason}"
            )
            if wants_json:
                return jsonify(payload)
            return redirect(url_for("candidate_profile"))

        message = "已加入本地投递清单；当前 Agent 可运行 venv/bin/python run.py --handoff-list 读取。"
        payload = _attempt_public_payload(attempt, message=message)
        if wants_json:
            return jsonify(payload)
        return redirect(url_for("dashboard_view", saved="1"))
    except (KeyError, OSError, ValueError) as exc:
        return failure(str(exc))


@app.route("/applypilot", methods=["GET"])
def applypilot_view():
    return redirect(url_for("dashboard_view"))


@app.route("/attempts", methods=["GET"])
def attempts_legacy():
    return redirect(url_for("dashboard_view"))


@app.route("/applypilot/<attempt_id>/handoff", methods=["GET"])
def applypilot_handoff(attempt_id: str):
    cfg = load_config()
    try:
        return jsonify(_attempts(cfg).handoff_payload(attempt_id))
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404


@app.route("/applypilot/<attempt_id>/advance", methods=["POST"])
@app.route("/attempts/<attempt_id>/advance", methods=["POST"])
def advance_attempt(attempt_id: str):
    cfg = load_config()
    try:
        _attempts(cfg).advance(
            attempt_id, request.form.get("status", ""), actor="user",
            reason=request.form.get("reason", ""),
            submission_confirmed="submission_confirmed" in request.form,
            submission_evidence=request.form.get("submission_evidence", ""),
        )
    except (KeyError, ValueError) as exc:
        return redirect(url_for("dashboard_view", error=str(exc)))
    return redirect(url_for("dashboard_view"))


@app.route("/save", methods=["POST"])
def save():
    cfg = load_config()

    cfg["search"]["terms"] = [
        t.strip() for t in request.form.get("terms", "").splitlines() if t.strip()
    ]
    cfg["search"]["location"] = request.form.get("location", cfg["search"]["location"])
    cfg["search"]["hours_old"] = int(request.form.get("hours_old", cfg["search"]["hours_old"]))

    cfg["sources"]["linkedin"] = "src_linkedin" in request.form
    cfg["sources"]["indeed"] = "src_indeed" in request.form
    cfg["sources"]["seek"] = "src_seek" in request.form

    cfg["visa"]["exclude_keywords"] = [
        k.strip() for k in request.form.get("exclude_keywords", "").splitlines() if k.strip()
    ]
    cfg["visa"]["bonus_keywords"] = [
        k.strip() for k in request.form.get("bonus_keywords", "").splitlines() if k.strip()
    ]

    cfg["llm"]["provider"] = request.form.get("provider", cfg["llm"]["provider"])
    cfg["scoring"]["generate_threshold"] = int(
        request.form.get("threshold", cfg["scoring"]["generate_threshold"])
    )

    save_config(cfg)

    prefs_text = request.form.get("preferences", "")
    if prefs_text.strip():
        PREFS_PATH.write_text(prefs_text, encoding="utf-8")

    return redirect(url_for("index", saved="1"))


@app.route("/run", methods=["POST"])
def run():
    data = request.get_json(force=True) or {}
    mode = data.get("mode")
    api_key = (data.get("api_key") or "").strip()
    generate = bool(data.get("generate"))
    try:
        limit = int(data.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0

    with RUN_LOCK:
        if RUN_STATE["running"]:
            return jsonify({"error": "已经有一个任务在跑,等它跑完再试"}), 409

    cfg = load_config()
    provider = cfg["llm"]["provider"]
    config_arg = str(CONFIG_PATH)

    if mode == "scrape":
        cmd = [sys.executable, "run.py", "--config", config_arg, "--scrape-only"]
    elif mode == "full":
        cmd = [sys.executable, "run.py", "--config", config_arg]
        if generate:
            cmd.append("--generate")
    elif mode == "rescore":
        cmd = [sys.executable, "run.py", "--config", config_arg, "--from-cache"]
        if generate:
            cmd.append("--generate")
    else:
        return jsonify({"error": f"未知模式: {mode}"}), 400

    # 数量上限:抓取时收窄每词抓取量、打分时只处理前 N 个,搜索和打分都更快
    if limit and limit > 0:
        cmd += ["--limit", str(limit)]

    env_overrides = {}
    if api_key:
        env_overrides[_provider_key_env_name(provider)] = api_key

    thread = threading.Thread(target=_run_pipeline, args=(cmd, env_overrides), daemon=True)
    thread.start()
    return jsonify({"ok": True})


@app.route("/run/status", methods=["GET"])
def run_status():
    with RUN_LOCK:
        response = jsonify({
            "running": RUN_STATE["running"],
            "log": RUN_STATE["log"][-200:],
            "returncode": RUN_STATE["returncode"],
        })
    response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


def _score_int(row) -> int:
    for key in ("job_fit_score", "match_score", "score"):
        v = str(row.get(key) or "").strip()
        if not v:
            continue
        try:
            return int(v)
        except ValueError:
            continue
    return -999


@app.route("/results", methods=["GET"])
def results():
    # Keep old bookmarks working after the result list was merged into Dashboard.
    return redirect(url_for("dashboard_view"))


if __name__ == "__main__":
    print()
    print("=" * 60)
    print("jobhunt-au 设置面板启动了")
    print("浏览器打开: http://127.0.0.1:5050")
    print("Ctrl+C 停止")
    print("=" * 60)
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
