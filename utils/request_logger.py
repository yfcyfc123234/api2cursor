"""对话级文件日志

将同一段多轮对话聚合到一个 JSON 文件中，而不是按单次请求散落成多个文件。
仅在详细日志模式开启时记录。
日志目录: data/conversations/YYYY-MM-DD/{conversation_id}.json
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import queue
import threading
from datetime import datetime
from typing import Any

from config import Config
from settings import DATA_DIR
import settings
from utils.http import gen_id

logger = logging.getLogger(__name__)

_LOG_DIR = os.path.join(DATA_DIR, 'conversations')
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_STREAM_KEEP_HEAD = 12
_STREAM_KEEP_TAIL = 12

# 设为 1/true 时，verbose 模式下写入磁盘的流式事件不再做头尾折叠（完整保留，文件可能非常大）。
# 用于离线分析 Cursor ↔ 上游 LLM 的完整交互；实时 SSE 预览仍可能截断字符串长度。
_FULL_STREAM_DISK = os.getenv('VERBOSE_FULL_STREAM', '').lower() in ('1', 'true', 'yes', 'on')

# ─── 实时日志（verbose 模式） ─────────────────────────
#
# 实时日志用于管理面板在“正在生成”时可见 request/response 的过程。
# 由于仅在 verbose 模式才会产生日志，因此这里做了尽量轻量的事件预览：
# - streaming 事件按序号采样（可通过 LIVE_LOG_STREAM_EVENT_SAMPLE_N 调整）
# - payload 统一截断为字符串预览，避免过大导致队列膨胀/前端卡顿

_LIVE_SUBSCRIBERS_LOCK = threading.Lock()
_LIVE_SUBSCRIBERS: set[queue.Queue] = set()

_LIVE_SUB_QUEUE_MAXSIZE = int(os.getenv('LIVE_LOG_SUB_QUEUE_MAXSIZE', '500'))
_LIVE_STREAM_EVENT_SAMPLE_N = int(os.getenv('LIVE_LOG_STREAM_EVENT_SAMPLE_N', '1'))
_LIVE_PAYLOAD_MAX_CHARS = int(os.getenv('LIVE_LOG_PAYLOAD_MAX_CHARS', '3000'))


def register_live_subscriber() -> queue.Queue:
    """注册一个实时日志订阅者（每个 SSE 连接一个队列）。"""
    q: queue.Queue = queue.Queue(maxsize=_LIVE_SUB_QUEUE_MAXSIZE)
    with _LIVE_SUBSCRIBERS_LOCK:
        _LIVE_SUBSCRIBERS.add(q)
    return q


def unregister_live_subscriber(q: queue.Queue) -> None:
    """移除实时日志订阅者。"""
    with _LIVE_SUBSCRIBERS_LOCK:
        _LIVE_SUBSCRIBERS.discard(q)


def _truncate_preview(value: Any, *, max_chars: int = _LIVE_PAYLOAD_MAX_CHARS) -> str:
    """把任意对象转换为截断后的字符串预览。"""
    try:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)

    if len(text) > max_chars:
        return text[:max_chars] + '...[truncated]'
    return text


def _emit_live_event(*, kind: str, turn: dict[str, Any], payload: Any) -> None:
    """向所有订阅者广播一条实时日志事件。"""
    if settings.get_debug_mode() != 'verbose':
        return

    event = {
        'type': 'log_event',
        'ts': datetime.utcnow().isoformat() + 'Z',
        'kind': kind,
        'conversation_id': turn.get('conversation_id', ''),
        'turn_id': turn.get('turn_id', ''),
        'route': turn.get('route', ''),
        'client_model': turn.get('client_model', ''),
        'backend': turn.get('backend', ''),
        'stream': bool(turn.get('stream', False)),
        'payload': _truncate_preview(payload),
    }

    with _LIVE_SUBSCRIBERS_LOCK:
        # 注意：不做深拷贝，payload 已经截断为字符串预览
        subscribers = list(_LIVE_SUBSCRIBERS)

    for q in subscribers:
        try:
            q.put_nowait(event)
        except queue.Full:
            # 队列满则丢弃旧事件（不阻塞主线程）
            pass


def start_turn(
    *,
    route: str,
    client_model: str,
    backend: str,
    stream: bool,
    client_request: dict[str, Any],
    request_headers: dict[str, Any] | None = None,
    target_url: str = '',
    upstream_model: str = '',
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """创建一条新的对话 turn 上下文。"""
    if settings.get_debug_mode() != 'verbose':
        return None

    now = datetime.utcnow().isoformat() + 'Z'
    conversation_id = get_conversation_id(route=route, payload=client_request)
    turn_id = gen_id('turn_')
    turn = {
        'conversation_id': conversation_id,
        'turn_id': turn_id,
        'route': route,
        'client_model': client_model,
        'backend': backend,
        'stream': stream,
        'target_url': target_url,
        'upstream_model': upstream_model,
        'started_at': now,
        'updated_at': now,
        'request_headers': sanitize_headers(request_headers or {}),
        'client_request': deep_copy_jsonable(client_request),
        'metadata': deep_copy_jsonable(metadata or {}),
        'upstream_request': None,
        'upstream_response': None,
        'client_response': None,
        'stream_trace': {
            'upstream_events': [],
            'client_events': [],
            'upstream_total': 0,
            'client_total': 0,
            'upstream_dropped': 0,
            'client_dropped': 0,
            'summary': {},
        },
        'error': None,
    }
    _emit_turn_started_if_needed(turn)
    return turn


def _emit_turn_started_if_needed(turn: dict[str, Any]) -> None:
    """在开始 turn 时推送一条轻量事件。"""
    # 为减少开销，不直接推整段 client_request
    try:
        payload = {
            'client_request_keys': list((turn.get('client_request') or {}).keys()),
            'metadata': turn.get('metadata', {}),
        }
        _emit_live_event(kind='turn_started', turn=turn, payload=payload)
    except Exception:
        # 实时日志不应影响主链路
        pass


def get_conversation_id(*, route: str, payload: dict[str, Any]) -> str:
    """尽量为同一段多轮对话生成稳定的会话 ID。"""
    explicit = _pick_explicit_conversation_id(payload)
    if explicit:
        return _safe_id(explicit)

    seed = _conversation_seed(route, payload)
    digest = hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]
    return f'conv_{digest}'


def attach_upstream_request(turn: dict[str, Any] | None, payload: dict[str, Any], headers: dict[str, Any] | None = None) -> None:
    """记录最终发往上游的请求。"""
    if turn is None:
        return
    turn['upstream_request'] = {
        'headers': sanitize_headers(headers or {}),
        'body': deep_copy_jsonable(payload),
    }
    _touch(turn)
    _emit_live_event(kind='upstream_request', turn=turn, payload=turn['upstream_request'])


def attach_upstream_response(turn: dict[str, Any] | None, response_data: Any) -> None:
    """记录上游完整非流式响应。"""
    if turn is None:
        return
    turn['upstream_response'] = deep_copy_jsonable(response_data)
    _touch(turn)
    _emit_live_event(kind='upstream_response', turn=turn, payload=turn['upstream_response'])


def attach_client_response(turn: dict[str, Any] | None, response_data: Any) -> None:
    """记录最终返回给客户端的完整响应。"""
    if turn is None:
        return
    turn['client_response'] = deep_copy_jsonable(response_data)
    _touch(turn)
    _emit_live_event(kind='client_response', turn=turn, payload=turn['client_response'])


def append_upstream_event(turn: dict[str, Any] | None, event: Any) -> None:
    """记录一条上游流式事件，超限时截断保留头尾。"""
    if turn is None:
        return
    _append_stream_event(turn['stream_trace'], 'upstream', deep_copy_jsonable(event))
    _touch(turn)
    # streaming 事件可能频繁：按序号采样减少开销
    try:
        seq = int(turn['stream_trace'].get('upstream_total', 0))
        if seq == 1 or _LIVE_STREAM_EVENT_SAMPLE_N <= 1 or seq % _LIVE_STREAM_EVENT_SAMPLE_N == 0:
            _emit_live_event(kind='upstream_event', turn=turn, payload={'seq': seq, 'event': event})
    except Exception:
        pass


def append_client_event(turn: dict[str, Any] | None, event: Any) -> None:
    """记录一条返回给客户端的流式事件，超限时截断保留头尾。"""
    if turn is None:
        return
    _append_stream_event(turn['stream_trace'], 'client', deep_copy_jsonable(event))
    _touch(turn)
    try:
        seq = int(turn['stream_trace'].get('client_total', 0))
        if seq == 1 or _LIVE_STREAM_EVENT_SAMPLE_N <= 1 or seq % _LIVE_STREAM_EVENT_SAMPLE_N == 0:
            _emit_live_event(kind='client_event', turn=turn, payload={'seq': seq, 'event': event})
    except Exception:
        pass


def set_stream_summary(turn: dict[str, Any] | None, summary: dict[str, Any]) -> None:
    """记录流式摘要，例如累计文本、事件数、usage 等。"""
    if turn is None:
        return
    turn['stream_trace']['summary'] = deep_copy_jsonable(summary)
    _touch(turn)
    _emit_live_event(kind='stream_summary', turn=turn, payload=summary)


def attach_error(turn: dict[str, Any] | None, error: Any) -> None:
    """记录错误信息。"""
    if turn is None:
        return
    turn['error'] = deep_copy_jsonable(error)
    _touch(turn)
    _emit_live_event(kind='error', turn=turn, payload=turn['error'])


def finalize_turn(
    turn: dict[str, Any] | None,
    *,
    usage: dict[str, Any] | None = None,
    duration_ms: int = 0,
) -> None:
    """将 turn 追加/更新到对应的会话日志文件。"""
    if turn is None or settings.get_debug_mode() != 'verbose':
        return

    turn['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    turn['duration_ms'] = duration_ms
    if usage is not None:
        turn['usage'] = deep_copy_jsonable(usage)

    stream_trace = turn.get('stream_trace', {})
    summary = stream_trace.setdefault('summary', {})
    summary['upstream_total'] = stream_trace.get('upstream_total', 0)
    summary['client_total'] = stream_trace.get('client_total', 0)
    summary['upstream_dropped'] = stream_trace.get('upstream_dropped', 0)
    summary['client_dropped'] = stream_trace.get('client_dropped', 0)
    if stream_trace.get('upstream_dropped', 0) or stream_trace.get('client_dropped', 0):
        summary['truncated'] = True

    threading.Thread(target=_write_turn, args=(deep_copy_jsonable(turn),), daemon=True).start()
    try:
        _emit_live_event(
            kind='turn_done',
            turn=turn,
            payload={
                'duration_ms': duration_ms,
                'usage': usage,
                'error': bool(turn.get('error')),
            },
        )
    except Exception:
        pass


def sanitize_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """对敏感请求头做脱敏。"""
    sanitized: dict[str, Any] = {}
    for key, value in headers.items():
        key_lower = str(key).lower()
        if key_lower in {'authorization', 'x-api-key', 'api-key', 'x-goog-api-key'}:
            sanitized[key] = _mask_secret(value)
        else:
            sanitized[key] = value
    return sanitized


def deep_copy_jsonable(value: Any) -> Any:
    """尽量深拷贝 JSON 兼容数据。"""
    try:
        return copy.deepcopy(value)
    except Exception:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False, default=str))
        except Exception:
            return str(value)


def _write_turn(turn: dict[str, Any]) -> None:
    conversation_id = turn['conversation_id']
    lock = _get_lock(conversation_id)
    with lock:
        try:
            date_str = turn['started_at'][:10]
            day_dir = os.path.join(_LOG_DIR, date_str)
            os.makedirs(day_dir, exist_ok=True)
            filepath = os.path.join(day_dir, f'{conversation_id}.json')

            if os.path.exists(filepath):
                with open(filepath, 'r', encoding='utf-8') as f:
                    doc = json.load(f)
            else:
                doc = {
                    'conversation_id': conversation_id,
                    'route': turn.get('route', ''),
                    'created_at': turn['started_at'],
                    'updated_at': turn['updated_at'],
                    'turns': [],
                }

            turns = doc.setdefault('turns', [])
            replaced = False
            for index, existing in enumerate(turns):
                if existing.get('turn_id') == turn.get('turn_id'):
                    turns[index] = turn
                    replaced = True
                    break
            if not replaced:
                turns.append(turn)

            doc['updated_at'] = turn['updated_at']
            doc['last_client_model'] = turn.get('client_model', '')
            doc['last_backend'] = turn.get('backend', '')
            doc['turn_count'] = len(turns)

            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
        except OSError as e:
            logger.warning('写入对话日志失败: %s', e)
        except json.JSONDecodeError as e:
            logger.warning('解析对话日志失败: %s', e)


def _get_lock(conversation_id: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if conversation_id not in _LOCKS:
            _LOCKS[conversation_id] = threading.Lock()
        return _LOCKS[conversation_id]


def _append_stream_event(stream_trace: dict[str, Any], kind: str, event: Any) -> None:
    events_key = f'{kind}_events'
    total_key = f'{kind}_total'
    dropped_key = f'{kind}_dropped'

    events = stream_trace.setdefault(events_key, [])
    stream_trace[total_key] = stream_trace.get(total_key, 0) + 1

    if _FULL_STREAM_DISK:
        events.append(event)
        return

    # 前 KEEP_HEAD 条完整保留；之后只保留最后 KEEP_TAIL 条，
    # 中间部分通过 dropped 计数折叠，避免文件膨胀。
    if len(events) < (_STREAM_KEEP_HEAD + _STREAM_KEEP_TAIL):
        events.append(event)
        return

    head = events[:_STREAM_KEEP_HEAD]
    tail = events[_STREAM_KEEP_HEAD:]
    if len(tail) >= _STREAM_KEEP_TAIL:
        tail.pop(0)
        stream_trace[dropped_key] = stream_trace.get(dropped_key, 0) + 1
    tail.append(event)
    stream_trace[events_key] = head + tail


def _touch(turn: dict[str, Any] | None) -> None:
    if turn is None:
        return
    turn['updated_at'] = datetime.utcnow().isoformat() + 'Z'


def _pick_explicit_conversation_id(payload: dict[str, Any]) -> str:
    candidates = (
        payload.get('conversation_id'),
        payload.get('conversationId'),
        payload.get('session_id'),
        payload.get('sessionId'),
        payload.get('chat_id'),
        payload.get('chatId'),
        payload.get('metadata', {}).get('conversation_id') if isinstance(payload.get('metadata'), dict) else None,
        payload.get('metadata', {}).get('session_id') if isinstance(payload.get('metadata'), dict) else None,
    )
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ''


def _conversation_seed(route: str, payload: dict[str, Any]) -> str:
    """生成稳定的对话种子。

    关键原则：不能直接把整段历史消息都放进 seed，
    否则每一轮历史增长都会导致 conversation_id 改变，最终每次请求都新建文件。

    这里改为基于“对话根消息”生成种子：
    - chat/messages: 第一条 user + 第一条 assistant（没有 assistant 时退化为第一条 user）
    - responses: input 中的第一条 user + 第一条 assistant（没有 assistant 时退化为第一条 user）
    """
    if route == 'chat':
        return 'chat|' + _root_seed_from_messages(payload.get('messages', []))

    if route == 'responses':
        return 'responses|' + _root_seed_from_responses_input(payload)

    if route == 'messages':
        system = payload.get('system', '')
        root = _root_seed_from_messages(payload.get('messages', []))
        return 'messages|' + str(system) + '|' + root

    return route + '|' + _pick_explicit_conversation_id(payload)


def _root_seed_from_messages(messages: Any) -> str:
    if not isinstance(messages, list):
        return ''

    first_user = None
    first_assistant = None
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get('role', '')
        if role in ('system', 'developer'):
            continue
        normalized = {
            'role': role,
            'content': _normalize_content(msg.get('content')),
            'tool_call_id': msg.get('tool_call_id', ''),
            'tool_calls': [
                {
                    'id': tc.get('id', ''),
                    'name': (tc.get('function') or {}).get('name', ''),
                }
                for tc in msg.get('tool_calls', [])
                if isinstance(tc, dict)
            ],
        }
        if role == 'user' and first_user is None:
            first_user = normalized
        elif role == 'assistant' and first_assistant is None:
            first_assistant = normalized
        if first_user is not None and first_assistant is not None:
            break

    seed_parts = []
    if first_user is not None:
        seed_parts.append(first_user)
    if first_assistant is not None:
        seed_parts.append(first_assistant)
    return json.dumps(seed_parts, ensure_ascii=False, separators=(',', ':'))


def _root_seed_from_responses_input(payload: dict[str, Any]) -> str:
    instructions = payload.get('instructions') or ''
    input_data = payload.get('input', [])

    if isinstance(input_data, str):
        seed_input = input_data
    elif isinstance(input_data, list):
        seed_input = _root_seed_from_responses_items(input_data)
    else:
        seed_input = json.dumps(input_data, ensure_ascii=False, default=str)

    return instructions + '|' + seed_input


def _root_seed_from_responses_items(items: list[Any]) -> str:
    first_user = None
    first_assistant = None

    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get('type', '')
        role = item.get('role', '')

        if item_type in ('message', 'input_text', 'output_text'):
            normalized = {
                'type': item_type,
                'role': role,
                'content': _normalize_content(
                    item.get('content')
                    or item.get('text')
                    or item.get('input_text')
                    or item.get('output_text')
                    or ''
                ),
            }
            if role == 'user' and first_user is None:
                first_user = normalized
            elif role == 'assistant' and first_assistant is None:
                first_assistant = normalized

        elif item_type == 'function_call' and first_assistant is None:
            first_assistant = {
                'type': 'function_call',
                'name': item.get('name', ''),
                'call_id': item.get('call_id', ''),
            }

        if first_user is not None and first_assistant is not None:
            break

    seed_parts = []
    if first_user is not None:
        seed_parts.append(first_user)
    if first_assistant is not None:
        seed_parts.append(first_assistant)
    return json.dumps(seed_parts, ensure_ascii=False, separators=(',', ':'))


def _normalize_messages_seed(messages: Any) -> str:
    if not isinstance(messages, list):
        return ''
    normalized: list[dict[str, Any]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        normalized.append({
            'role': msg.get('role', ''),
            'content': _normalize_content(msg.get('content')),
            'tool_call_id': msg.get('tool_call_id', ''),
            'tool_calls': [
                {
                    'id': tc.get('id', ''),
                    'name': (tc.get('function') or {}).get('name', ''),
                }
                for tc in msg.get('tool_calls', [])
                if isinstance(tc, dict)
            ],
        })
    return json.dumps(normalized, ensure_ascii=False, separators=(',', ':'))


def _normalize_content(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append(str(item))
        return result
    if content is None:
        return ''
    return str(content)


def _safe_id(raw: str) -> str:
    cleaned = ''.join(ch if ch.isalnum() or ch in ('-', '_', '.') else '_' for ch in raw.strip())
    return cleaned[:120] or gen_id('conv_')


def _mask_secret(value: Any) -> str:
    text = str(value or '')
    if len(text) <= 8:
        return '***'
    return text[:4] + '***' + text[-4:]
