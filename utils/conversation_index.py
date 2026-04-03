"""会话日志索引（SQLite）

只存「相对路径 + 元数据」，完整 JSON 仍在 data/conversations/ 下。
管理列表、计数、导出筛选、最近可疑会话等优先走索引；必要时全量重建与磁盘对齐。

环境变量：
  CONVERSATION_INDEX_PATH  — SQLite 文件绝对或相对路径；未设则使用 data/conversation_index.sqlite3
  CONVERSATION_INDEX_DISABLED — 设为 1/true 时关闭索引（回退为纯 glob，练手调试用）
"""

from __future__ import annotations

import glob
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from config import Config
import settings

logger = logging.getLogger(__name__)

_DB_LOCK = threading.Lock()
_SCHEMA_VERSION = 2

def conversations_root() -> str:
    return os.path.join(settings.DATA_DIR, 'conversations')


def _disabled() -> bool:
    return bool(getattr(Config, 'CONVERSATION_INDEX_DISABLED', False))


def _db_path() -> str:
    raw = (getattr(Config, 'CONVERSATION_INDEX_PATH', None) or os.getenv('CONVERSATION_INDEX_PATH', '') or '').strip()
    if raw:
        return raw if os.path.isabs(raw) else os.path.join(settings.DATA_DIR, raw)
    return os.path.join(settings.DATA_DIR, 'conversation_index.sqlite3')


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_db_path()) or '.', exist_ok=True)
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=60.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_index (
            conversation_id TEXT PRIMARY KEY,
            rel_path TEXT NOT NULL,
            date TEXT NOT NULL,
            updated_at TEXT,
            created_at TEXT,
            ts_min TEXT,
            ts_max TEXT,
            route TEXT,
            last_client_model TEXT,
            last_backend TEXT,
            turn_count INTEGER NOT NULL DEFAULT 0,
            has_error INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ci_updated ON conversation_index(updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_ci_date ON conversation_index(date);
        CREATE INDEX IF NOT EXISTS idx_ci_has_err ON conversation_index(has_error, updated_at DESC);
        """
    )
    cur = conn.execute('PRAGMA user_version')
    ver = cur.fetchone()[0]
    if ver < _SCHEMA_VERSION:
        conn.execute(f'PRAGMA user_version = {_SCHEMA_VERSION}')
    conn.commit()


def _parse_iso_dt(s: str) -> datetime | None:
    if not s or not str(s).strip():
        return None
    text = str(s).strip()
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _collect_doc_times(doc: dict[str, Any]) -> list[datetime]:
    times: list[datetime] = []
    for k in ('created_at', 'updated_at'):
        t = _parse_iso_dt(str(doc.get(k) or ''))
        if t:
            times.append(t)
    for turn in doc.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        for k in ('started_at', 'updated_at'):
            t = _parse_iso_dt(str(turn.get(k) or ''))
            if t:
                times.append(t)
    return times


def _doc_has_turn_error(doc: dict[str, Any]) -> bool:
    for turn in doc.get('turns') or []:
        if not isinstance(turn, dict):
            continue
        err = turn.get('error')
        if err is None:
            continue
        if isinstance(err, dict) and not err:
            continue
        if isinstance(err, str) and not err.strip():
            continue
        return True
    return False


def _row_from_doc(doc: dict[str, Any], rel_path: str) -> tuple[Any, ...]:
    cid = str(doc.get('conversation_id') or '').strip()
    if not cid:
        stem = os.path.splitext(os.path.basename(rel_path))[0]
        cid = stem
    parts = rel_path.replace('\\', '/').split('/')
    date = parts[-2] if len(parts) >= 2 else ''
    times = _collect_doc_times(doc)
    ts_min = min(times).isoformat().replace('+00:00', 'Z') if times else None
    ts_max = max(times).isoformat().replace('+00:00', 'Z') if times else None
    return (
        cid,
        rel_path.replace('\\', '/'),
        date,
        str(doc.get('updated_at') or '') or None,
        str(doc.get('created_at') or '') or None,
        ts_min,
        ts_max,
        str(doc.get('route') or '') or None,
        str(doc.get('last_client_model') or '') or None,
        str(doc.get('last_backend') or '') or None,
        int(doc.get('turn_count') or 0),
        1 if _doc_has_turn_error(doc) else 0,
    )


def initialize() -> None:
    """应用启动时：建表；若索引为空但磁盘上已有 json，则全量重建。"""
    if _disabled():
        logger.info('conversation_index: 已禁用 (CONVERSATION_INDEX_DISABLED)')
        return
    try:
        os.makedirs(settings.DATA_DIR, exist_ok=True)
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                n = conn.execute('SELECT COUNT(*) FROM conversation_index').fetchone()[0]
            finally:
                conn.close()
        conv = conversations_root()
        if not os.path.isdir(conv):
            return
        disk_n = len(glob.glob(os.path.join(conv, '*', '*.json')))
        if n == 0 and disk_n > 0:
            logger.info('conversation_index: 索引为空，从磁盘重建 (%d 个文件)', disk_n)
            rebuild_from_disk()
    except OSError as e:
        logger.warning('conversation_index: 初始化失败 %s', e)


def upsert_from_document(doc: dict[str, Any], abs_path: str) -> None:
    """在成功写入会话 JSON 后调用，同步一行索引。"""
    if _disabled():
        return
    try:
        conv = conversations_root()
        rel = os.path.relpath(abs_path, conv).replace('\\', '/')
        row = _row_from_doc(doc, rel)
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute(
                    """INSERT OR REPLACE INTO conversation_index
                    (conversation_id, rel_path, date, updated_at, created_at, ts_min, ts_max,
                     route, last_client_model, last_backend, turn_count, has_error)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    row,
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: upsert 失败 %s', e)


def delete_conversation(conversation_id: str) -> None:
    if _disabled():
        return
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute('DELETE FROM conversation_index WHERE conversation_id = ?', (conversation_id,))
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: delete 失败 %s', e)


def clear_all_rows() -> None:
    if _disabled():
        return
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                conn.execute('DELETE FROM conversation_index')
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: clear 失败 %s', e)


def count_rows() -> int | None:
    """返回索引行数；禁用时返回 None 表示调用方应改用 glob。"""
    if _disabled():
        return None
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                n = conn.execute('SELECT COUNT(*) FROM conversation_index').fetchone()[0]
                return int(n)
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: count 失败 %s', e)
        return None


def rebuild_from_disk() -> int:
    """清空索引并按当前磁盘上的 json 全量重建。返回成功编入条数。"""
    if _disabled():
        return 0
    conv = conversations_root()
    if not os.path.isdir(conv):
        clear_all_rows()
        return 0
    files = glob.glob(os.path.join(conv, '*', '*.json'))
    n_ok = 0
    with _DB_LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            conn.execute('DELETE FROM conversation_index')
            for fp in files:
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        doc = json.load(f)
                    if not isinstance(doc, dict):
                        continue
                    rel = os.path.relpath(fp, conv).replace('\\', '/')
                    row = _row_from_doc(doc, rel)
                    conn.execute(
                        """INSERT OR REPLACE INTO conversation_index
                        (conversation_id, rel_path, date, updated_at, created_at, ts_min, ts_max,
                         route, last_client_model, last_backend, turn_count, has_error)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        row,
                    )
                    n_ok += 1
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
            conn.commit()
        finally:
            conn.close()
    logger.info('conversation_index: 重建完成，共 %d 条', n_ok)
    return n_ok


def resolve_abs_path(conversation_id: str, date: str | None = None) -> str | None:
    """由 conversation_id（及可选 date）得到绝对路径；不存在则 None。"""
    if _disabled():
        return None
    conv = conversations_root()
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                if date:
                    row = conn.execute(
                        'SELECT rel_path FROM conversation_index WHERE conversation_id = ? AND date = ?',
                        (conversation_id, date),
                    ).fetchone()
                else:
                    row = conn.execute(
                        'SELECT rel_path FROM conversation_index WHERE conversation_id = ? '
                        'ORDER BY (updated_at IS NULL), updated_at DESC LIMIT 1',
                        (conversation_id,),
                    ).fetchone()
                if not row:
                    return None
                rel = row['rel_path']
                abs_p = os.path.normpath(os.path.join(conv, rel.replace('/', os.sep)))
                if not abs_p.startswith(os.path.normpath(conv)):
                    return None
                return abs_p if os.path.isfile(abs_p) else None
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: resolve 失败 %s', e)
        return None


def list_admin_rows(*, limit: int, q: str, date: str | None) -> list[dict[str, Any]] | None:
    """供管理页列表使用；禁用时返回 None。"""
    if _disabled():
        return None
    try:
        params: list[Any] = []
        where = ['1=1']
        if date:
            where.append('date = ?')
            params.append(date)
        if q:
            like = f'%{q}%'
            where.append(
                '(conversation_id LIKE ? OR IFNULL(route,"") LIKE ? OR IFNULL(last_client_model,"") LIKE ?)'
            )
            params.extend([like, like, like])
        sql = (
            f"SELECT * FROM conversation_index WHERE {' AND '.join(where)} "
            'ORDER BY (updated_at IS NULL), updated_at DESC LIMIT ?'
        )
        params.append(limit)
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                cur = conn.execute(sql, params)
                rows = cur.fetchall()
            finally:
                conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                'conversation_id': r['conversation_id'],
                'date': r['date'],
                'route': r['route'] or '',
                'last_client_model': r['last_client_model'] or '',
                'last_backend': r['last_backend'] or '',
                'created_at': r['created_at'] or '',
                'updated_at': r['updated_at'] or '',
                'turn_count': int(r['turn_count'] or 0),
                '_rel_path': r['rel_path'],
            })
        return out
    except Exception as e:
        logger.warning('conversation_index: list 失败 %s', e)
        return None


def list_all_rel_paths() -> list[str] | None:
    """导出「全部」时的相对路径列表（无序）；失败或禁用返回 None。"""
    if _disabled():
        return None
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                cur = conn.execute('SELECT rel_path FROM conversation_index')
                return [str(r[0]) for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: list_all_rel_paths 失败 %s', e)
        return None


def list_rel_paths_time_range_overlap(start_iso: str, end_iso: str) -> list[str] | None:
    """与 [start_iso, end_iso] 时间范围（ISO 字符串比较）有交集的会话文件相对路径。"""
    if _disabled():
        return None
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                cur = conn.execute(
                    """
                    SELECT rel_path FROM conversation_index
                    WHERE ts_min IS NOT NULL AND ts_max IS NOT NULL
                      AND ts_max >= ? AND ts_min <= ?
                    """,
                    (start_iso, end_iso),
                )
                return [str(r[0]) for r in cur.fetchall()]
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: time_range 失败 %s', e)
        return None


def pick_last_suspect_rel_path() -> tuple[str | None, str]:
    """返回 (rel_path, reason)；reason: has_turn_error | newest_fallback | no_files | not_indexed"""
    if _disabled():
        return None, 'not_indexed'
    try:
        with _DB_LOCK:
            conn = _connect()
            try:
                _ensure_schema(conn)
                row = conn.execute(
                    """
                    SELECT rel_path FROM conversation_index
                    WHERE has_error = 1
                    ORDER BY (updated_at IS NULL), updated_at DESC LIMIT 1
                    """
                ).fetchone()
                if row:
                    return str(row[0]), 'has_turn_error'
                row = conn.execute(
                    'SELECT rel_path FROM conversation_index '
                    'ORDER BY (updated_at IS NULL), updated_at DESC LIMIT 1'
                ).fetchone()
                if row:
                    return str(row[0]), 'newest_fallback'
                return None, 'no_files'
            finally:
                conn.close()
    except Exception as e:
        logger.warning('conversation_index: pick_last_suspect 失败 %s', e)
        return None, 'not_indexed'


def abs_path_from_rel(rel: str) -> str:
    return os.path.normpath(os.path.join(conversations_root(), rel.replace('/', os.sep)))
