"""会话日志 SQLite 存储

替代原有的 JSON 文件存储，提供：
  - 去重消息存储（每轮 turn 的消息只存一次，不再随对话膨胀）
  - 流式事件按行存储（逐条索引，回放时按需取）
  - 全文搜索（消息内容、错误信息、模型名）
  - 原子写入（SQLite WAL 模式，无需文件锁）

数据库文件: data/conversation_store.sqlite3 (与现有 conversation_index.sqlite3 合并)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time as _time_mod
from datetime import datetime, timezone
from typing import Any

from settings import DATA_DIR
from utils.http import gen_id

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
_SCHEMA_VERSION = 1

_STORE_PATH = os.path.join(DATA_DIR, 'conversations.db')


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_STORE_PATH, check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    """创建全部表与索引（幂等）。"""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            route TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_client_model TEXT NOT NULL DEFAULT '',
            last_backend TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0,
            has_error INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            turn_index INTEGER NOT NULL,
            route TEXT NOT NULL DEFAULT '',
            client_model TEXT NOT NULL DEFAULT '',
            backend TEXT NOT NULL DEFAULT '',
            upstream_model TEXT NOT NULL DEFAULT '',
            target_url TEXT NOT NULL DEFAULT '',
            stream INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            error_stage TEXT,
            error_message TEXT,
            stream_summary TEXT,
            client_response TEXT,
            upstream_response TEXT,
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL,
            msg_index INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            content_type TEXT NOT NULL DEFAULT 'text',
            tool_calls_json TEXT,
            tool_call_id TEXT,
            FOREIGN KEY (turn_id) REFERENCES turns(id)
        );

        CREATE TABLE IF NOT EXISTS upstream_requests (
            turn_id TEXT PRIMARY KEY,
            headers TEXT,
            body TEXT,
            FOREIGN KEY (turn_id) REFERENCES turns(id)
        );

        CREATE TABLE IF NOT EXISTS stream_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            turn_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            seq INTEGER NOT NULL,
            event_type TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL,
            FOREIGN KEY (turn_id) REFERENCES turns(id)
        );

        CREATE INDEX IF NOT EXISTS idx_turns_conv ON turns(conversation_id, turn_index);
        CREATE INDEX IF NOT EXISTS idx_msgs_turn ON messages(turn_id, msg_index);
        CREATE INDEX IF NOT EXISTS idx_stream_turn ON stream_events(turn_id, kind, seq);
        CREATE INDEX IF NOT EXISTS idx_conv_updated ON conversations(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_conv_error ON conversations(has_error, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_conv_model ON conversations(last_client_model);
        CREATE INDEX IF NOT EXISTS idx_turns_error ON turns(error_stage);
    """)
    cur = conn.execute('PRAGMA user_version')
    ver = cur.fetchone()[0]
    if ver < _SCHEMA_VERSION:
        conn.execute(f'PRAGMA user_version = {_SCHEMA_VERSION}')
    conn.commit()


# ═══════════════════════════════════════════
#  写入
# ═══════════════════════════════════════════

def upsert_conversation(conv_id: str, route: str, created_at: str) -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            conn.execute(
                """INSERT INTO conversations (id, route, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at""",
                (conv_id, route, created_at, created_at),
            )
            conn.commit()
        finally:
            conn.close()


def insert_turn(turn: dict[str, Any]) -> None:
    """写入一个完整 turn（含消息、上游请求、流式事件）。"""
    conv_id = turn['conversation_id']
    turn_id = turn['turn_id']
    stream = 1 if turn.get('stream') else 0
    error = turn.get('error')
    error_stage = None
    error_message = None
    if error:
        if isinstance(error, dict):
            error_stage = str(error.get('stage', '')) or None
            error_message = str(error.get('message', '')) or None
        else:
            error_message = str(error)

    usage = turn.get('usage') or {}
    stream_summary = None
    st = turn.get('stream_trace', {})
    if st.get('summary'):
        stream_summary = _safe_json(st['summary'])

    client_response = _safe_json(turn.get('client_response'))
    upstream_response = _safe_json(turn.get('upstream_response'))

    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)

            # 1. 确保 conversation 行存在
            conn.execute(
                """INSERT OR IGNORE INTO conversations (id, route, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (conv_id, turn.get('route', ''), turn['started_at'], turn['started_at']),
            )

            # 2. upsert turn
            conn.execute(
                """INSERT OR REPLACE INTO turns
                   (id, conversation_id, turn_index, route, client_model, backend,
                    upstream_model, target_url, stream, started_at, updated_at,
                    duration_ms, prompt_tokens, completion_tokens, total_tokens,
                    error_stage, error_message, stream_summary, client_response, upstream_response)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    turn_id, conv_id,
                    _compute_turn_index(conn, conv_id, turn_id),
                    turn.get('route', ''), turn.get('client_model', ''),
                    turn.get('backend', ''), turn.get('upstream_model', ''),
                    turn.get('target_url', ''), stream,
                    turn['started_at'], turn.get('updated_at', turn['started_at']),
                    turn.get('duration_ms', 0),
                    usage.get('prompt_tokens', 0), usage.get('completion_tokens', 0),
                    usage.get('total_tokens', 0),
                    error_stage, error_message,
                    stream_summary, client_response, upstream_response,
                ),
            )

            # 3. 删除该 turn 旧数据再写入（幂等）
            conn.execute('DELETE FROM messages WHERE turn_id = ?', (turn_id,))
            conn.execute('DELETE FROM upstream_requests WHERE turn_id = ?', (turn_id,))
            conn.execute('DELETE FROM stream_events WHERE turn_id = ?', (turn_id,))

            # 4. 写入消息
            cr = turn.get('client_request') or {}
            msgs = cr.get('messages') or []
            for idx, msg in enumerate(msgs):
                content = msg.get('content')
                if isinstance(content, (list, dict)):
                    content_type = 'json'
                    content_str = _safe_json(content)
                else:
                    content_type = 'text'
                    content_str = str(content) if content is not None else ''

                tool_calls_json = _safe_json(msg.get('tool_calls'))

                conn.execute(
                    """INSERT INTO messages
                       (turn_id, msg_index, role, content, content_type, tool_calls_json, tool_call_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (turn_id, idx, msg.get('role', ''), content_str, content_type,
                     tool_calls_json, msg.get('tool_call_id', '')),
                )

            # 5. 写入上游请求
            ur = turn.get('upstream_request') or {}
            if ur:
                conn.execute(
                    "INSERT INTO upstream_requests (turn_id, headers, body) VALUES (?, ?, ?)",
                    (turn_id, _safe_json(ur.get('headers')), _safe_json(ur.get('body'))),
                )

            # 6. 写入流式事件
            for kind in ('upstream', 'client'):
                events = st.get(f'{kind}_events', [])
                for seq, evt in enumerate(events):
                    evt_type = ''
                    if isinstance(evt, dict):
                        evt_type = evt.get('type', '')
                    conn.execute(
                        "INSERT INTO stream_events (turn_id, kind, seq, event_type, data) VALUES (?, ?, ?, ?, ?)",
                        (turn_id, kind, seq, evt_type, _safe_json(evt)),
                    )

            # 7. 更新 conversation 元数据
            conn.execute(
                """UPDATE conversations
                   SET updated_at = ?, last_client_model = ?, last_backend = ?,
                       turn_count = (SELECT COUNT(*) FROM turns WHERE conversation_id = ?),
                       has_error = (SELECT CASE WHEN COUNT(*) > 0 THEN 1 ELSE 0 END
                                    FROM turns WHERE conversation_id = ? AND error_stage IS NOT NULL)
                   WHERE id = ?""",
                (turn.get('updated_at', turn['started_at']),
                 turn.get('client_model', ''), turn.get('backend', ''),
                 conv_id, conv_id, conv_id),
            )
            conn.commit()
        finally:
            conn.close()


def _compute_turn_index(conn: sqlite3.Connection, conv_id: str, turn_id: str) -> int:
    """为 turn 分配递增索引（相同 conversation 内从 0 开始）。"""
    row = conn.execute(
        "SELECT MAX(turn_index) FROM turns WHERE conversation_id = ? AND id != ?",
        (conv_id, turn_id),
    ).fetchone()
    max_idx = row[0]
    if max_idx is not None:
        return max_idx + 1
    return 0


def _safe_json(val: Any) -> str | None:
    if val is None:
        return None
    try:
        return json.dumps(val, ensure_ascii=False, default=str)
    except Exception:
        return str(val)


# ═══════════════════════════════════════════
#  读取
# ═══════════════════════════════════════════

def list_conversations(*, limit: int = 50, q: str = '', date: str = '',
                       sort_field: str = 'updated_at', sort_dir: str = 'desc') -> list[dict[str, Any]]:
    """列出会话（用于管理面板列表）。"""
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            allowed_sort = {'updated_at', 'created_at', 'turn_count', 'last_client_model', 'last_backend'}
            sf = sort_field if sort_field in allowed_sort else 'updated_at'
            sd = 'DESC' if sort_dir.lower() == 'desc' else 'ASC'

            where = ['1=1']
            params: list[Any] = []
            if date:
                where.append("date(updated_at) = ?")
                params.append(date)
            if q:
                like = f'%{q}%'
                where.append(
                    "(id LIKE ? OR route LIKE ? OR last_client_model LIKE ? OR last_backend LIKE ?)")
                params.extend([like, like, like, like])

            sql = f"SELECT * FROM conversations WHERE {' AND '.join(where)} ORDER BY {sf} {sd} LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def get_conversation_meta(conv_id: str) -> dict[str, Any] | None:
    """获取会话的元数据摘要（含 turn 列表，不含消息体）。"""
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not conv:
                return None
            result = dict(conv)
            turns = conn.execute(
                """SELECT id, turn_index, route, client_model, backend, upstream_model,
                          stream, started_at, updated_at, duration_ms,
                          prompt_tokens, completion_tokens, total_tokens,
                          error_stage, error_message, stream_summary
                   FROM turns WHERE conversation_id = ?
                   ORDER BY turn_index""",
                (conv_id,),
            ).fetchall()
            result['turns_summary'] = [dict(t) for t in turns]
            return result
        finally:
            conn.close()


def get_turn_detail(conv_id: str, turn_index: int | None = None, turn_id: str | None = None) -> dict[str, Any] | None:
    """获取单个 turn 的完整数据（含消息、上游请求、流式事件）。"""
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)

            conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
            if not conv:
                return None

            # 定位 turn
            if turn_id:
                turn_row = conn.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
            elif turn_index is not None:
                turn_row = conn.execute(
                    "SELECT * FROM turns WHERE conversation_id = ? AND turn_index = ?",
                    (conv_id, turn_index),
                ).fetchone()
            else:
                turn_row = conn.execute(
                    "SELECT * FROM turns WHERE conversation_id = ? ORDER BY turn_index LIMIT 1",
                    (conv_id,),
                ).fetchone()
            if not turn_row:
                return None

            turn = dict(turn_row)
            total_turns = conn.execute(
                "SELECT COUNT(*) FROM turns WHERE conversation_id = ?", (conv_id,)
            ).fetchone()[0]

            # 消息
            msgs = conn.execute(
                "SELECT * FROM messages WHERE turn_id = ? ORDER BY msg_index", (turn_row['id'],)
            ).fetchall()
            messages = []
            for m in msgs:
                msg = {
                    'role': m['role'],
                    'content': _parse_json_or_text(m['content'], m['content_type']),
                }
                if m['tool_call_id']:
                    msg['tool_call_id'] = m['tool_call_id']
                tc = _parse_json(m['tool_calls_json'])
                if tc:
                    msg['tool_calls'] = tc
                messages.append(msg)

            # 上游请求
            ur = conn.execute(
                "SELECT * FROM upstream_requests WHERE turn_id = ?", (turn_row['id'],)
            ).fetchone()

            # 流式事件
            stream_events_upstream = []
            stream_events_client = []
            for row in conn.execute(
                "SELECT * FROM stream_events WHERE turn_id = ? ORDER BY kind, seq",
                (turn_row['id'],),
            ):
                evt = _parse_json(row['data'])
                if evt is None:
                    evt = row['data']
                if row['kind'] == 'upstream':
                    stream_events_upstream.append(evt)
                else:
                    stream_events_client.append(evt)

            # 组装返回
            return {
                'conversation_id': conv_id,
                'route': conv['route'],
                'created_at': conv['created_at'],
                'updated_at': conv['updated_at'],
                'last_client_model': conv['last_client_model'],
                'last_backend': conv['last_backend'],
                '_current_turn': turn.get('turn_index', 0),
                '_total_turns': total_turns,
                'turns': [{
                    'turn_id': turn_row['id'],
                    'route': turn['route'],
                    'client_model': turn['client_model'],
                    'backend': turn['backend'],
                    'upstream_model': turn['upstream_model'],
                    'target_url': turn['target_url'],
                    'stream': bool(turn['stream']),
                    'started_at': turn['started_at'],
                    'updated_at': turn['updated_at'],
                    'duration_ms': turn['duration_ms'],
                    'usage': {
                        'prompt_tokens': turn['prompt_tokens'],
                        'completion_tokens': turn['completion_tokens'],
                        'total_tokens': turn['total_tokens'],
                    },
                    'error': {'stage': turn['error_stage'], 'message': turn['error_message']}
                              if turn['error_stage'] or turn['error_message'] else None,
                    'client_request': {'messages': messages},
                    'upstream_request': {'headers': _parse_json(ur['headers']) if ur else None,
                                         'body': _parse_json(ur['body']) if ur else None},
                    'stream_trace': {
                        'upstream_events': stream_events_upstream,
                        'client_events': stream_events_client,
                        'summary': _parse_json(turn['stream_summary']) or {},
                    },
                    'client_response': _parse_json(turn['client_response']),
                    'upstream_response': _parse_json(turn['upstream_response']),
                }],
            }
        finally:
            conn.close()


def get_conversation_count() -> int:
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            return conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        finally:
            conn.close()


def delete_conversation(conv_id: str) -> None:
    """级联删除会话、turn、消息、事件。"""
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            for turn_row in conn.execute(
                "SELECT id FROM turns WHERE conversation_id = ?", (conv_id,)
            ):
                tid = turn_row['id']
                conn.execute("DELETE FROM messages WHERE turn_id = ?", (tid,))
                conn.execute("DELETE FROM upstream_requests WHERE turn_id = ?", (tid,))
                conn.execute("DELETE FROM stream_events WHERE turn_id = ?", (tid,))
            conn.execute("DELETE FROM turns WHERE conversation_id = ?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            conn.commit()
        finally:
            conn.close()


def clear_all() -> None:
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            conn.execute("DELETE FROM stream_events")
            conn.execute("DELETE FROM upstream_requests")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM conversations")
            conn.commit()
        finally:
            conn.close()


def search_messages(keyword: str, limit: int = 50) -> list[dict[str, Any]]:
    """全文搜索消息内容，返回匹配的 turn 列表。"""
    with _DB_LOCK:
        conn = _connect()
        try:
            ensure_schema(conn)
            like = f'%{keyword}%'
            rows = conn.execute(
                """SELECT DISTINCT t.conversation_id, t.id as turn_id, t.turn_index,
                          t.client_model, t.started_at, m.role, m.content
                   FROM messages m
                   JOIN turns t ON m.turn_id = t.id
                   WHERE m.content LIKE ? AND m.content_type = 'text'
                   ORDER BY t.started_at DESC
                   LIMIT ?""",
                (like, limit),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                content = d.get('content', '')
                if len(content) > 300:
                    snippet_idx = content.lower().find(keyword.lower())
                    start = max(0, snippet_idx - 100)
                    d['snippet'] = '...' + content[start:start + 300] + '...'
                else:
                    d['snippet'] = content
                del d['content']
                results.append(d)
            return results
        finally:
            conn.close()


def _parse_json(val: str | None) -> Any:
    if not val:
        return None
    try:
        return json.loads(val)
    except (json.JSONDecodeError, TypeError):
        return val


def _parse_json_or_text(val: str | None, content_type: str) -> Any:
    if val is None:
        return ''
    if content_type == 'json':
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val
