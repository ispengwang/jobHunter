"""LLM provider 抽象 —— Anthropic / OpenAI / DeepSeek 三选一,接口一致。

只暴露一个 complete(system, user) -> str。
JSON 解析单独抽出来,因为模型偶尔会在 JSON 外面裹一层 markdown code fence。

DeepSeek 用的是 OpenAI 兼容接口,复用 openai 这个包,只是换 base_url。
2026-07-22 核实:DeepSeek 的旧模型名 deepseek-chat / deepseek-reasoner
将于 2026-07-24 弃用,换成 deepseek-v4-flash / deepseek-v4-pro,这里直接用新名字。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


class LLM:
    def __init__(self, cfg: dict):
        llm_cfg = cfg.get("llm", {})
        self.provider = llm_cfg.get("provider", "anthropic").lower()
        self.max_description_chars = llm_cfg.get("max_description_chars", 4000)
        self.batch_size = llm_cfg.get("batch_size", 8)

        if self.provider == "anthropic":
            import anthropic
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("缺少环境变量 ANTHROPIC_API_KEY")
            self._client = anthropic.Anthropic(api_key=key)
            self.model = llm_cfg.get("anthropic_model", "claude-sonnet-5")
        elif self.provider == "openai":
            from openai import OpenAI
            key = os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("缺少环境变量 OPENAI_API_KEY")
            self._client = OpenAI(api_key=key)
            self.model = llm_cfg.get("openai_model", "gpt-4o")
        elif self.provider == "deepseek":
            # DeepSeek 是 OpenAI 兼容接口,复用同一个 SDK,换 base_url 和 key 就行
            from openai import OpenAI
            key = os.environ.get("DEEPSEEK_API_KEY")
            if not key:
                raise RuntimeError("缺少环境变量 DEEPSEEK_API_KEY")
            self._client = OpenAI(api_key=key, base_url="https://api.deepseek.com")
            # 便宜、够用,几百个岗位打分这种任务不需要上 pro 档
            self.model = llm_cfg.get("deepseek_model", "deepseek-v4-flash")
        else:
            raise ValueError(f"未知 provider: {self.provider}")

        log.info("LLM: %s / %s", self.provider, self.model)

    def complete(self, system: str, user: str, max_tokens: int = 4096) -> str:
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")

        # openai 和 deepseek 走同一套 chat.completions 接口
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return resp.choices[0].message.content or ""

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> Optional[Any]:
        raw = self.complete(
            system + "\n\n只输出合法 JSON,不要任何解释文字,不要 markdown code fence。",
            user,
            max_tokens,
        )
        return extract_json(raw)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Optional[Any]:
    """模型输出的 JSON 经常裹在 code fence 里或前后带解释,尽量捞出来。"""
    if not text:
        return None

    for candidate in _candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    log.warning("JSON 解析失败,原始输出前 300 字符:%s", text[:300])
    return None


def _candidates(text: str):
    yield text.strip()

    m = _FENCE_RE.search(text)
    if m:
        yield m.group(1).strip()

    # 退而求其次:截取第一个 [ 或 { 到最后一个 ] 或 }
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = text.find(open_c), text.rfind(close_c)
        if i != -1 and j > i:
            yield text[i:j + 1]
