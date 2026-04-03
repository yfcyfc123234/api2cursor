"""轻量 Thinking 缓存

纯内存缓存，在多轮对话中保存和恢复 thinking/reasoning 内容。
解决 Cursor 不会把 thinking 内容回传给 API 的问题，
某些模型（如推理模型）在缺少历史 thinking 时表现会下降。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r'<redacted_thinking>.*?</redacted_thinking>', re.DOTALL)


def fold_chat_completion_stream_chunks(
    chunks: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """将发给客户端的 chat.completion.chunk 列表折叠为一条 assistant 消息（用于 thinking 缓存键）。

    与 OpenAI 流式增量格式一致：按 index 合并 tool_calls，拼接 content / reasoning_content。
    """
    reasoning_parts: list[str] = []
    content_parts: list[str] = []
    tc_by_index: dict[int, dict[str, Any]] = {}

    for chunk in chunks:
        for choice in chunk.get('choices') or []:
            delta = choice.get('delta') or {}
            rc = delta.get('reasoning_content')
            if rc:
                reasoning_parts.append(rc)
            ct = delta.get('content')
            if ct:
                content_parts.append(ct)
            for tc in delta.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                idx = int(tc.get('index', 0))
                cur = tc_by_index.setdefault(
                    idx,
                    {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}},
                )
                if tc.get('id'):
                    cur['id'] = tc['id']
                if tc.get('type'):
                    cur['type'] = tc['type']
                fn = tc.get('function') or {}
                if isinstance(fn, dict):
                    if fn.get('name'):
                        cur['function']['name'] = fn['name']
                    if fn.get('arguments'):
                        cur['function']['arguments'] = cur['function'].get('arguments', '') + str(
                            fn['arguments']
                        )

    reasoning = ''.join(reasoning_parts).strip()
    content = ''.join(content_parts)
    tool_calls = [tc_by_index[i] for i in sorted(tc_by_index.keys())]

    if not reasoning and not content and not tool_calls:
        return None

    msg: dict[str, Any] = {'role': 'assistant', 'content': content or None}
    if tool_calls:
        msg['tool_calls'] = tool_calls
    if reasoning:
        msg['reasoning_content'] = reasoning
    return msg
_UNCLOSED_THINK_RE = re.compile(r'<think>.*$', re.DOTALL)
_TOOL_ID_RE = re.compile(r'[^a-zA-Z0-9_-]')
_TTL = 86400  # 24 hours


class ThinkingCache:
    """纯内存 thinking 缓存，默认 TTL 24 小时。"""

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}

    def inject(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """遍历 assistant 消息，缺少 reasoning_content 时从缓存注入。"""
        sid = self._session_id(messages)
        if not sid:
            return messages

        now = time.time()
        for msg in messages:
            if msg.get('role') != 'assistant':
                continue
            if msg.get('reasoning_content'):
                continue
            key = sid + ':' + self._message_hash(msg)
            entry = self._store.get(key)
            if entry and (now - entry[1]) < _TTL:
                msg['reasoning_content'] = entry[0]
                logger.debug('已从缓存注入 thinking (%d 字符)', len(entry[0]))

        return messages

    def store_assistant_thinking(
        self,
        messages: list[dict[str, Any]],
        assistant_msg: dict[str, Any],
    ) -> None:
        """缓存本条 assistant 的 reasoning，供下一轮 inject 补回（Cursor 常不回传）。"""
        rc = assistant_msg.get('reasoning_content') or ''
        if not isinstance(rc, str):
            rc = str(rc)
        has_tools = bool(assistant_msg.get('tool_calls'))
        if not has_tools and not rc:
            return
        sid = self._session_id(messages)
        if not sid:
            return
        key = sid + ':' + self._message_hash(assistant_msg)
        # 上游若要求字段存在，允许写入空串（例如仅有 tool_calls 时）
        self._store[key] = (rc, time.time())
        self._cleanup()

    def _session_id(self, messages: list[dict[str, Any]]) -> str:
        """同一会话内稳定：只取首条非 system/developer 的 user 内容。

        旧逻辑要求「首条 user + 首条 assistant」同时存在才生成 sid，导致首轮补全（请求里尚无
        assistant）永远无法写入缓存，多轮 tool 场景下 Moonshot 等会缺 reasoning_content。
        """
        first_user = ''
        for msg in messages:
            role = msg.get('role', '')
            if role in ('system', 'developer'):
                continue
            if role == 'user':
                first_user = self._normalize_content(msg.get('content', ''))
                break

        if not first_user:
            return ''

        return hashlib.sha256(first_user.encode()).hexdigest()[:16]

    def _message_hash(self, msg: dict[str, Any]) -> str:
        content = self._normalize_content(msg.get('content', ''))
        tool_ids = sorted(
            self._normalize_tool_id(tc.get('id', ''))
            for tc in msg.get('tool_calls', [])
            if isinstance(tc, dict)
        )
        raw = json.dumps({'c': content, 't': tool_ids}, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _normalize_content(content: Any) -> str:
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict) and p.get('type') == 'text':
                    parts.append(p.get('text', ''))
                elif isinstance(p, str):
                    parts.append(p)
            text = '\n'.join(parts)
        elif isinstance(content, str):
            text = content
        else:
            text = str(content) if content else ''
        text = _THINK_RE.sub('', text)
        text = _UNCLOSED_THINK_RE.sub('', text)
        return text.strip()

    @staticmethod
    def _normalize_tool_id(tid: str) -> str:
        return _TOOL_ID_RE.sub('', tid)

    def _cleanup(self) -> None:
        """惰性清理过期条目（每 100 次写入触发一次全量扫描）。"""
        if len(self._store) < 100:
            return
        now = time.time()
        expired = [k for k, (_, ts) in self._store.items() if (now - ts) >= _TTL]
        for k in expired:
            del self._store[k]


thinking_cache = ThinkingCache()
