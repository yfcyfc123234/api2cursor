"""路由层公共辅助

收敛多个数据面路由都会用到的上下文解析、上游目标构造、日志输出和
SSE 消息拼装逻辑，避免 `chat.py` 和 `responses.py` 各自维护重复实现。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from typing import Any

import settings
from utils.http import build_anthropic_headers, build_gemini_headers, build_openai_headers

logger = logging.getLogger(__name__)
from utils.log_categories import log as cat_log


@dataclass(frozen=True)
class RouteContext:
    """数据面路由使用的标准请求上下文。

    路由层会先根据客户端模型名解析出统一上下文，后续处理函数只需要关心
    上游模型、后端类型、目标地址、鉴权信息、流式标记和自定义指令，
    而不必重复访问配置层。
    """

    client_model: str
    upstream_model: str
    backend: str
    target_url: str
    api_key: str
    is_stream: bool
    custom_instructions: str
    instructions_position: str
    body_modifications: dict
    header_modifications: dict
    reasoning_to_content: bool = False  # 将上游 reasoning_content 复制到 content 供 Cursor 显示


def build_route_context(client_model: str, is_stream: bool) -> RouteContext:
    """解析模型映射，得到当前请求的统一路由上下文。"""
    mapping = settings.resolve_model(client_model)
    return RouteContext(
        client_model=client_model,
        upstream_model=mapping['upstream_model'],
        backend=mapping['backend'],
        target_url=mapping['target_url'],
        api_key=mapping['api_key'],
        is_stream=is_stream,
        custom_instructions=mapping.get('custom_instructions', ''),
        instructions_position=mapping.get('instructions_position', 'prepend'),
        body_modifications=mapping.get('body_modifications', {}),
        header_modifications=mapping.get('header_modifications', {}),
        reasoning_to_content=mapping.get('reasoning_to_content', False),
    )


def build_openai_target(ctx: RouteContext) -> tuple[str, dict[str, str]]:
    """根据路由上下文生成 OpenAI 兼容后端的地址和请求头。"""
    url = f'{ctx.target_url.rstrip("/")}/v1/chat/completions'
    headers = build_openai_headers(ctx.api_key)
    return url, headers


def build_responses_target(ctx: RouteContext) -> tuple[str, dict[str, str]]:
    """根据路由上下文生成 OpenAI Responses 后端的地址和请求头。"""
    url = f'{ctx.target_url.rstrip("/")}/v1/responses'
    headers = build_openai_headers(ctx.api_key)
    return url, headers


def build_anthropic_target(ctx: RouteContext) -> tuple[str, dict[str, str]]:
    """根据路由上下文生成 Anthropic 后端的地址和请求头。"""
    url = f'{ctx.target_url.rstrip("/")}/v1/messages'
    headers = build_anthropic_headers(ctx.api_key)
    return url, headers


def build_gemini_target(ctx: RouteContext, stream: bool = False) -> tuple[str, dict[str, str]]:
    """根据路由上下文生成 Gemini 后端的地址和请求头。

    Gemini URL 格式: {base}/v1/models/{model}:generateContent
    流式: {base}/v1/models/{model}:streamGenerateContent?alt=sse
    """
    base = ctx.target_url.rstrip('/')
    model = ctx.upstream_model
    if stream:
        url = f'{base}/v1/models/{model}:streamGenerateContent?alt=sse'
    else:
        url = f'{base}/v1/models/{model}:generateContent'
    headers = build_gemini_headers(ctx.api_key)
    return url, headers


def log_route_context(route_name: str, ctx: RouteContext, *, extra: str = '') -> None:
    """统一输出路由级日志，避免不同入口的日志格式逐渐漂移。"""
    parts = [
        f'[{route_name}]',
        f'模型={ctx.client_model}',
        f'上游模型={ctx.upstream_model}',
        f'后端={ctx.backend}',
        f'流式={ctx.is_stream}',
    ]
    if extra:
        parts.append(extra)
    cat_log('proxy', ' '.join(parts))


def log_usage(
    route_name: str,
    usage: dict[str, Any],
    *,
    input_key: str,
    output_key: str,
) -> None:
    """统一输出令牌统计日志。

    不同协议对 usage 字段命名不一致，这里只接收字段名，不在调用方重复拼接日志文案。
    """
    logger.info(
        '[%s] 请求完成 输入令牌=%s 输出令牌=%s',
        route_name,
        usage.get(input_key, 0),
        usage.get(output_key, 0),
    )


def sse_data_message(data: Any) -> str:
    """构造仅包含 data 的 SSE 消息。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f'data: {payload}\n\n'


def sse_event_message(event_type: str, data: Any) -> str:
    """构造带 event 名称的 SSE 消息。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f'event: {event_type}\ndata: {payload}\n\n'


def chat_error_chunk(message: str, error_type: str = 'upstream_error') -> str:
    """构造聊天补全流式接口使用的错误消息。"""
    return sse_data_message({'error': {'message': message, 'type': error_type}})


def format_upstream_error_for_cursor(error_str: str) -> str:
    """将上游原始错误解析并格式化为 Cursor 可正确显示的 SSE 错误块。

    当前 chat_error_chunk 会把上游 JSON 错误体直接塞进 message 字段，
    导致 Cursor 端错误信息不可读。此函数尝试解析上游 JSON 并提取
    有意义的错误消息，然后格式化为规范的 OpenAI 错误格式。
    """
    import json as _json

    try:
        # 错误格式: "上游错误 429: {...json...}"
        json_start = error_str.find('{')
        if json_start >= 0:
            upstream_body = _json.loads(error_str[json_start:])
            if isinstance(upstream_body, dict):
                # Anthropic: {"type":"error","error":{"type":"...","message":"..."}}
                # OpenAI:   {"error": {"message": "...", "type": "..."}}
                inner = upstream_body.get('error', upstream_body)
                if isinstance(inner, dict):
                    message = inner.get('message', str(upstream_body))
                    error_type = inner.get('type', 'upstream_error')
                else:
                    message = str(upstream_body)
                    error_type = 'upstream_error'
                return sse_data_message({
                    'error': {
                        'message': message,
                        'type': error_type,
                    }
                })
    except (_json.JSONDecodeError, ValueError, AttributeError):
        pass

    # Fallback: 尝试去掉 "上游错误 xxx: " 前缀
    clean_msg = error_str
    if error_str.startswith('上游错误'):
        colon_idx = error_str.find(':')
        if colon_idx > 0:
            clean_msg = error_str[colon_idx + 1:].strip()

    return sse_data_message({
        'error': {
            'message': clean_msg,
            'type': 'upstream_error',
        }
    })


def responses_error_event(message: str) -> str:
    """构造 Responses 流式接口使用的错误事件。"""
    return sse_event_message('error', {'error': message})


# ─── 自定义指令注入 ──────────────────────────────


def _merge_text(custom: str, existing: str, position: str) -> str:
    """根据 position 决定自定义指令与原有内容的拼接顺序。"""
    if not existing:
        return custom
    if position == 'append':
        return existing + '\n\n' + custom
    return custom + '\n\n' + existing


def inject_instructions_cc(payload: dict[str, Any], instructions: str, position: str = 'prepend') -> dict[str, Any]:
    """向 Chat Completions 请求注入自定义指令。

    position='prepend' 时放在 system 消息开头，'append' 时放在末尾。
    """
    if not instructions:
        return payload

    messages = payload.get('messages', [])
    if messages and messages[0].get('role') == 'system':
        first = messages[0]
        original = first.get('content') or ''
        first['content'] = _merge_text(instructions, original, position)
    else:
        messages.insert(0, {'role': 'system', 'content': instructions})
        payload['messages'] = messages

    logger.info('已注入自定义指令到 CC system 消息 (%d 字符, %s)', len(instructions), position)
    return payload


def inject_instructions_responses(payload: dict[str, Any], instructions: str, position: str = 'prepend') -> dict[str, Any]:
    """向 Responses 请求注入自定义指令（写入 instructions 字段）。

    position='prepend' 时放在 instructions 开头，'append' 时放在末尾。
    """
    if not instructions:
        return payload

    existing = payload.get('instructions') or ''
    payload['instructions'] = _merge_text(instructions, existing, position)

    logger.info('已注入自定义指令到 Responses instructions (%d 字符, %s)', len(instructions), position)
    return payload


def inject_instructions_anthropic(payload: dict[str, Any], instructions: str, position: str = 'prepend') -> dict[str, Any]:
    """向 Anthropic Messages 请求注入自定义指令（写入 system 字段）。

    position='prepend' 时放在 system 开头，'append' 时放在末尾。
    """
    if not instructions:
        return payload

    existing = payload.get('system') or ''
    if isinstance(existing, list):
        existing = '\n'.join(
            block.get('text', '') for block in existing
            if isinstance(block, dict) and block.get('type') == 'text'
        )
    payload['system'] = _merge_text(instructions, existing, position)

    logger.info('已注入自定义指令到 Anthropic system (%d 字符, %s)', len(instructions), position)
    return payload


# ─── Body / Header 修改 ──────────────────────────


def ensure_prompt_cache_key(payload: dict[str, Any]) -> dict[str, Any]:
    """确保 Responses 请求携带 prompt_cache_key，以便上游启用提示缓存。

    部分上游对原生 /v1/responses 不会自动生成 prompt_cache_key，导致相同
    系统提示的多轮对话无法命中缓存前缀。这里在客户端未显式提供时，按
    model + instructions 生成稳定短哈希；已有值则原样保留。
    """
    if payload.get('prompt_cache_key'):
        return payload

    model = payload.get('model', '') or ''
    instructions = payload.get('instructions', '') or ''
    if not isinstance(instructions, str):
        instructions = json.dumps(instructions, ensure_ascii=False, sort_keys=True)
    seed = f'{model}|{instructions}'
    payload['prompt_cache_key'] = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]
    return payload


def apply_body_modifications(payload: dict[str, Any], modifications: dict[str, Any]) -> dict[str, Any]:
    """对转发请求体应用字段级修改。

    规则与 CursorProxy 一致：值为 null 的字段会被删除，其余字段设置/覆盖。
    """
    if not modifications:
        return payload
    for key, value in modifications.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    logger.info('已应用 body_modifications: %s', list(modifications.keys()))
    return payload


def apply_header_modifications(headers: dict[str, str], modifications: dict[str, Any]) -> dict[str, str]:
    """对转发请求头应用字段级修改。

    规则同 body：值为 null 删除，其余设置/覆盖。
    """
    if not modifications:
        return headers
    for key, value in modifications.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = str(value)
    logger.info('已应用 header_modifications: %s', list(modifications.keys()))
    return headers


# 余额/资源包不足错误模式 —— 这类错误重试多少次都没用，直接放弃
import re as _re
_BILLING_RE = _re.compile(
    r'1113|余额不足|无可用的资源包|insufficient_balance|insufficient_quota|billing',
    _re.IGNORECASE,
)


def forward_with_patches(
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    upstream_model: str = '',
    client_type: str = '',
    stream: bool = False,
    max_retries: int = 2,
):
    """发送请求并自动打补丁重试。

    当上游返回错误且 error_patcher 有匹配补丁时，自动修复并重试。
    返回 (response, error, patch_logs, timing_dict)。
    timing_dict 包含: upstream_start_ms, upstream_ttfb_ms, upstream_total_ms
    """
    import time as _time
    from utils.http import forward_request
    from utils.error_patcher import match as match_patches, apply as apply_patch

    patches_applied = []
    current_payload = payload
    upstream_elapsed_ms = 0
    upstream_ttfb_ms = 0
    total_attempt_start = _time.time()

    for attempt in range(max_retries + 1):
        t0 = _time.time()
        resp, err = forward_request(url, headers, current_payload, stream=stream)
        upstream_elapsed_ms = int((_time.time() - t0) * 1000)
        # TTFB: 对于非流式就是总耗时，对于流式连接建立就是 TTFB
        upstream_ttfb_ms = upstream_elapsed_ms  # requests 库连接+响应头时间

        if not err:
            if patches_applied:
                cat_log('patch', '[补丁] 重试成功 (尝试 %d 次, 补丁: %s)',
                            attempt, ', '.join(p['name'] for p in patches_applied))
            timing = {
                'upstream_ttfb_ms': upstream_ttfb_ms,
                'upstream_total_ms': upstream_elapsed_ms,
                'attempts': attempt + 1,
                'retries': attempt,
            }
            return resp, None, patches_applied, timing

        # 最后一次尝试不重试
        if attempt >= max_retries:
            break

        # 余额/资源包不足 —— 不可重试，直接放弃
        error_str = str(err)
        if _BILLING_RE.search(error_str):
            cat_log('patch', '[补丁] 余额/资源包不足，放弃重试: %s', error_str[:200], level=logging.WARNING)
            break

        # 尝试匹配补丁
        patches = match_patches(error_str, upstream_model, client_type)
        if not patches:
            break

        # 只保留可重试的补丁；如果全不可重试则不重试
        retryable_patches = [p for p in patches if p.get('retryable', True)]
        if not retryable_patches:
            cat_log('patch', '[补丁] 匹配到 %d 个补丁但均不可重试，放弃重试: %s',
                        len(patches), error_str[:150])
            break

        patch_start = _time.time()
        for patch in retryable_patches:
            cat_log('patch', '[补丁] 匹配到错误: %s → 应用补丁: %s',
                           error_str[:150], patch['description'], level=logging.WARNING)
            try:
                current_payload = apply_patch(current_payload, patch, error_str)
                patches_applied.append(patch)
            except Exception as e:
                cat_log('patch', '[补丁] 应用失败: %s', e, level=logging.WARNING)
        patch_ms = int((_time.time() - patch_start) * 1000)

    # 所有重试都失败
    if patches_applied:
        cat_log('patch', '[补丁] 所有重试失败 (%d 次), 补丁: %s',
                       len(patches_applied),
                       ', '.join(p['name'] for p in patches_applied), level=logging.WARNING)
    timing = {
        'upstream_ttfb_ms': upstream_ttfb_ms,
        'upstream_total_ms': upstream_elapsed_ms,
        'attempts': max_retries + 1,
        'retries': max_retries,
        'patch_ms': patch_ms if patches_applied else 0,
        'error': str(err)[:200],
    }
    return None, err, patches_applied, timing
