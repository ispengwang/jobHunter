"""Run one ApplyPilot handoff in a private, serial Codex background worker.

The Dashboard remains the only user-facing interface. This module does not create a second job
ledger and never receives free-form browser instructions from the web request. It accepts only an
existing attempt id, then asks the bundled Codex CLI to consume JobHunter's handoff contract.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
from typing import Callable


ATTEMPT_ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")
BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def resolve_codex_executable(configured: str = "") -> Path:
    """Prefer the desktop app's current CLI; old PATH installations may reject current models."""
    candidates: list[Path] = []
    if configured.strip():
        candidates.append(Path(configured).expanduser())
    candidates.append(BUNDLED_CODEX)
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到可执行的 Codex CLI。请安装或更新 Codex Desktop，然后重新启动 JobHunter。"
    )


def build_agent_prompt(attempt_id: str, skill_path: Path, project_root: Path) -> str:
    """Build a fixed handoff prompt; no job or candidate text is interpolated."""
    if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
        raise ValueError("attempt_id 格式无效")
    skill_file = skill_path.resolve() / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(f"找不到 applypilot-au Skill：{skill_file}")
    return f"""使用 [$applypilot-au]({skill_file}) 处理一次由用户在 JobHunter Dashboard
明确点击发起的单岗位投递。

项目目录：{project_root.resolve()}
内部记录：{attempt_id}

先完整读取 Skill 及其要求的 platform-playbook、safety-and-boundaries 和
jobhunter-integration，然后运行：

venv/bin/python run.py --attempt-handoff {attempt_id}

本次只处理这一条内部记录，不重新搜索岗位、不重新打分，也不要创建另一套队列。
用户的点击只授权处理这个具体岗位；它不覆盖平台条款、安全门、每日硬上限或事实校验。
使用 Skill 指定的 browser/computer-control 能力和现有已登录会话。

- manual_submit：不得自动操作平台申请；通过 run.py --attempt-update 写入 needs_user，
  清楚说明用户需要完成什么。
- auto_if_allowed：先核验当前具体页面和所有自动提交门槛；只有全部通过时才填写和提交。
- 登录、验证码、2FA、未知必填题、声明、测评、权限提示或平台限制：立即 needs_user。
- 只有看到明确提交成功证据，才能写 submitted 并保存证据。
- 不要在输出或日志中打印候选人联系方式、原始简历内容、API key、Cookie 或令牌。

结束前必须通过 run.py --attempt-update 写回 submitted、needs_user 或 failed 中的一个
可审计结果；不要让记录停留在 selected。"""


@dataclass
class ExecutionSnapshot:
    attempt_id: str
    state: str
    queued_at: str
    started_at: str = ""
    finished_at: str = ""
    returncode: int | None = None
    error: str = ""

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


@dataclass
class _WorkItem:
    attempt_id: str
    skill_path: Path
    on_complete: Callable[[str, int, str], None] | None


class ApplyPilotExecutor:
    """Serialize browser-owning Codex runs so two clicks cannot fight over one browser session."""

    def __init__(
        self,
        project_root: Path,
        *,
        codex_executable: str = "",
        private_output_dir: Path | None = None,
    ):
        self.project_root = project_root.resolve()
        self.codex_executable = codex_executable
        self.private_output_dir = (
            private_output_dir or self.project_root / "data" / "applypilot-executions"
        )
        self._queue: queue.Queue[_WorkItem] = queue.Queue()
        self._snapshots: dict[str, ExecutionSnapshot] = {}
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None

    def submit(
        self,
        attempt_id: str,
        skill_path: Path,
        *,
        on_complete: Callable[[str, int, str], None] | None = None,
    ) -> ExecutionSnapshot:
        if not ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
            raise ValueError("attempt_id 格式无效")
        resolve_codex_executable(self.codex_executable)
        if not (skill_path / "SKILL.md").is_file():
            raise FileNotFoundError(f"找不到 applypilot-au Skill：{skill_path / 'SKILL.md'}")

        with self._lock:
            if attempt_id in self._pending:
                return self._copy_snapshot(self._snapshots[attempt_id])
            snapshot = ExecutionSnapshot(
                attempt_id=attempt_id,
                state="queued",
                queued_at=_now(),
            )
            self._snapshots[attempt_id] = snapshot
            self._pending.add(attempt_id)
            self._queue.put(_WorkItem(attempt_id, skill_path.resolve(), on_complete))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._worker_loop,
                    name="applypilot-executor",
                    daemon=True,
                )
                self._worker.start()
            return self._copy_snapshot(snapshot)

    def snapshot(self, attempt_id: str) -> ExecutionSnapshot | None:
        with self._lock:
            snapshot = self._snapshots.get(attempt_id)
            return self._copy_snapshot(snapshot) if snapshot else None

    def snapshots(self) -> dict[str, dict[str, str | int | None]]:
        with self._lock:
            return {
                attempt_id: snapshot.as_dict()
                for attempt_id, snapshot in self._snapshots.items()
            }

    @staticmethod
    def _copy_snapshot(snapshot: ExecutionSnapshot) -> ExecutionSnapshot:
        return ExecutionSnapshot(**snapshot.as_dict())

    def _worker_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._worker = None
                        return
                continue
            self._run(item)
            self._queue.task_done()

    def _run(self, item: _WorkItem) -> None:
        with self._lock:
            snapshot = self._snapshots[item.attempt_id]
            snapshot.state = "running"
            snapshot.started_at = _now()

        returncode = -1
        error = ""
        try:
            executable = resolve_codex_executable(self.codex_executable)
            prompt = build_agent_prompt(item.attempt_id, item.skill_path, self.project_root)
            self.private_output_dir.mkdir(parents=True, exist_ok=True)
            log_path = self.private_output_dir / f"{item.attempt_id}.log"
            command = [
                str(executable),
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "-C",
                str(self.project_root),
                prompt,
            ]
            with log_path.open("a", encoding="utf-8") as log:
                log.write(f"{_now()} start\n")
                process = subprocess.Popen(
                    command,
                    cwd=str(self.project_root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    start_new_session=True,
                )
                returncode = process.wait()
                log.write(f"{_now()} finish returncode={returncode}\n")
            if returncode:
                error = f"Codex Agent 退出码 {returncode}"
        except Exception as exc:  # The callback converts this into an auditable attempt failure.
            error = f"{type(exc).__name__}: {exc}"

        callback_error = ""
        if item.on_complete:
            try:
                item.on_complete(item.attempt_id, returncode, error)
            except Exception as exc:
                callback_error = f"；状态对账失败：{type(exc).__name__}: {exc}"
        error += callback_error

        with self._lock:
            snapshot = self._snapshots[item.attempt_id]
            snapshot.state = "finished" if returncode == 0 and not error else "failed"
            snapshot.finished_at = _now()
            snapshot.returncode = returncode
            snapshot.error = error
            self._pending.discard(item.attempt_id)
