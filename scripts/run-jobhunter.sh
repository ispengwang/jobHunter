#!/bin/bash
# launchd wrapper —— 加载 .env 后运行 run.py。
#
# 存在的理由：launchd 启动的进程不读取 shell profile，
# 写在 ~/.zshrc 里的 export 它一律看不到。定时任务需要的
# API key 只能从项目根目录的 .env 注入。
#
# 用法（由 plist 调用，也可手动跑）：
#   scripts/run-jobhunter.sh --incremental
#   scripts/run-jobhunter.sh
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

# 只检查存在性，绝不打印取值。
if [ -z "${DEEPSEEK_API_KEY:-}" ] \
    && [ -z "${ANTHROPIC_API_KEY:-}" ] \
    && [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] 配置错误：未找到任何 LLM API key。" >&2
    echo "请在 $PROJECT_ROOT/.env 中设置 DEEPSEEK_API_KEY（参考 .env.example）。" >&2
    echo "launchd 不会读取 shell profile，定时任务的 key 只能来自 .env。" >&2
    exit 78
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] start: run.py $*"
exec venv/bin/python run.py "$@"
