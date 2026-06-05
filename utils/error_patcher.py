"""上游错误补丁引擎

根据上游 LLM 类型和客户端类型，匹配已知错误模式并自动修复请求后重试。

架构:
  ErrorPatcher.match(error_body, upstream_llm, client_type) → PatchAction | None
  ErrorPatcher.apply(patch, payload) → patched_payload

补丁注册表按上游 LLM 分类，每个补丁包含:
  - pattern: 错误消息正则匹配
  - fix: 对 payload 的修复函数
  - description: 人类可读的描述
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

PatchAction = dict[str, Any]  # {'name': str, 'description': str, 'fix': callable, 'retryable': bool}

# ═══════════════════════════════════════════
#  补丁注册表
# ═══════════════════════════════════════════

_PATCHES: dict[str, list[dict[str, Any]]] = {}


def _register(upstream: str, pattern: str, description: str, fix_fn, retryable: bool = True):
    """注册一条补丁规则。"""
    _PATCHES.setdefault(upstream, []).append({
        'pattern': re.compile(pattern, re.IGNORECASE),
        'description': description,
        'fix': fix_fn,
        'retryable': retryable,
        'upstream': upstream,
    })


# ── DeepSeek 补丁 ──────────────────────────

def _fix_deepseek_image_url(payload: dict[str, Any], _error: str) -> dict[str, Any]:
    """将 messages 中的 image_url content 替换为文本占位 [图片]。

    DeepSeek 不支持 image_url content 类型，会返回:
    "unknown variant `image_url`, expected `text`"
    """
    messages = payload.get('messages', [])
    fixed_count = 0
    for msg in messages:
        content = msg.get('content')
        if isinstance(content, list):
            new_content = []
            has_image = False
            for part in content:
                if isinstance(part, dict) and part.get('type') == 'image_url':
                    has_image = True
                    fixed_count += 1
                else:
                    new_content.append(part)
            if has_image:
                new_content.insert(0, {'type': 'text', 'text': '[图片]'})
                msg['content'] = new_content
    if fixed_count:
        logger.info('[补丁] deepseek/image_url: 替换了 %d 个 image_url → [图片]', fixed_count)
    return payload


def _fix_deepseek_tool_args(payload: dict[str, Any], error: str) -> dict[str, Any]:
    """修复 tool_calls arguments 的 JSON 格式问题。

    DeepSeek 有时返回 tool_calls 的 arguments 引号不匹配或包含非法字符。
    """
    # 从错误消息中提取出错的 message 索引
    m = re.search(r'messages\[(\d+)\]', error)
    if not m:
        return payload
    msg_idx = int(m.group(1))
    messages = payload.get('messages', [])
    if msg_idx < len(messages):
        msg = messages[msg_idx]
        if isinstance(msg.get('content'), str) and msg['content'].startswith('['):
            # content 看起来像 JSON 数组但应该是 tool response，尝试修复
            try:
                parsed = json.loads(msg['content'])
                if isinstance(parsed, list):
                    msg['content'] = json.dumps(parsed, ensure_ascii=False)
                    logger.info('[补丁] deepseek/tool_args: 修复 messages[%d] content JSON 格式', msg_idx)
            except json.JSONDecodeError:
                pass
    return payload


_register(
    'deepseek',
    r"unknown variant.*image_url",
    '替换 image_url 为文本占位 [图片]',
    _fix_deepseek_image_url,
)
_register(
    'deepseek',
    r"Failed to deserialize.*tool.*argument",
    '修复 tool arguments JSON 格式',
    _fix_deepseek_tool_args,
)
_register(
    'deepseek',
    r"messages\[\d+\].*unknown variant",
    '替换不支持的 content 类型为 text',
    lambda p, e: _fix_deepseek_image_url(p, e),  # 复用
)

# ── Anthropic 补丁 ──────────────────────────

def _fix_anthropic_content_format(payload: dict[str, Any], _error: str) -> dict[str, Any]:
    """确保 messages content 符合 Anthropic 格式要求。

    Anthropic 要求 tool_result content 是字符串或 content block 数组。
    """
    messages = payload.get('messages', [])
    for msg in messages:
        if msg.get('role') == 'user' and isinstance(msg.get('content'), list):
            # 确保每项都有 type 字段
            for part in msg['content']:
                if isinstance(part, dict) and 'type' not in part:
                    if 'text' in part:
                        part['type'] = 'text'
                    elif 'source' in part:
                        part['type'] = 'image'
                    else:
                        part['type'] = 'text'
        if msg.get('role') == 'tool':
            content = msg.get('content')
            if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict):
                # Anthropic 期望 string
                text_val = content[0].get('text', '')
                if text_val:
                    msg['content'] = text_val
    return payload


_register(
    'anthropic',
    r'messages\[\d+\]\.content.*unexpected',
    '规范化 Anthropic content 格式',
    _fix_anthropic_content_format,
)

# ── 通用补丁 ────────────────────────────────

def _fix_rate_limit(_payload: dict[str, Any], _error: str) -> dict[str, Any]:
    """429 限流：不修改请求，仅标记需要重试。"""
    return _payload  # 请求本身不需要修改，只重试


_register(
    'generic',
    r'429|rate.?limit|too many requests',
    '遇到限流，自动重试',
    _fix_rate_limit,
    retryable=True,
)

_register(
    'generic',
    r'502.*bad gateway|503.*service unavailable|server.*error',
    '上游临时故障，自动重试',
    _fix_rate_limit,
    retryable=True,
)


# ═══════════════════════════════════════════
#  匹配 & 应用
# ═══════════════════════════════════════════

def _load_fixes_from_config() -> list[dict[str, Any]]:
    """从 error_fixes.json 加载手动标注的修复规则。"""
    import json, os
    from settings import DATA_DIR
    fp = os.path.join(DATA_DIR, 'error_fixes.json')
    if not os.path.exists(fp):
        return []
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get('fixes', []) if isinstance(data, dict) else []
    except Exception:
        return []


def match(error_body: str, upstream_llm: str, client_type: str = '') -> list[PatchAction]:
    """匹配错误消息，返回可应用的补丁列表。

    优先级：硬编码补丁 > error_fixes.json 手动规则 > 通用补丁
    返回的每个 PatchAction 可能含 `fix_id` 字段。
    """
    if not error_body:
        return []

    matched = []
    llm_key = _llm_key(upstream_llm)

    # 1. 硬编码补丁
    for patch in _PATCHES.get(llm_key, []) + _PATCHES.get('generic', []):
        if patch['pattern'].search(error_body):
            matched.append({
                'name': f"{patch['upstream']}:{patch['description']}",
                'description': patch['description'],
                'fix': patch['fix'],
                'retryable': patch['retryable'],
            })

    # 2. 手动标注的修复规则
    for fix in _load_fixes_from_config():
        if fix.get('status') != 'active':
            continue
        fix_llm = _llm_key(fix.get('upstream_llm', ''))
        if fix_llm != llm_key and fix_llm != 'generic':
            continue
        try:
            if re.search(fix.get('error_pattern', ''), error_body, re.IGNORECASE):
                matched.append({
                    'name': f"manual:{fix.get('problem', '')}",
                    'description': fix.get('fix_description', ''),
                    'fix': lambda p, e: p,  # 手动规则：标记但不修改请求
                    'retryable': False,
                    'fix_id': fix.get('id', ''),
                })
        except re.error:
            pass

    return matched


def apply(payload: dict[str, Any], patch: PatchAction, error: str = '') -> dict[str, Any]:
    """应用补丁到请求体，返回修改后的 payload。"""
    try:
        result = patch['fix'](json.loads(json.dumps(payload, ensure_ascii=False)), error)
        return result
    except Exception as e:
        logger.warning('[补丁] 应用失败 %s: %s', patch.get('name', '?'), e)
        return payload


def _llm_key(upstream_llm: str) -> str:
    """将上游模型名映射到补丁 key。"""
    llm_lower = upstream_llm.lower()
    if 'deepseek' in llm_lower:
        return 'deepseek'
    if 'claude' in llm_lower or 'anthropic' in llm_lower:
        return 'anthropic'
    if 'gemini' in llm_lower:
        return 'gemini'
    return 'generic'
