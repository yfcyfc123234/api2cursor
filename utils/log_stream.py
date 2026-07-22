"""实时日志广播（给状态仪表盘用）

通过 Python logging Handler 捕获所有日志 → 无阻塞写入环形缓冲 →
SSE 订阅者按需拉取。不影响主请求链路性能。
"""

from __future__ import annotations

import logging
import queue
import threading
import time as _time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_BUFFER = 200       # 环形缓冲最大行数
_MAX_SUBSCRIBERS = 5   # 最多订阅者
_Q_MAXSIZE = 500       # 单订阅者队列上限（超出丢弃）


class _LogRingBuffer:
    """固定大小的环形缓冲，存储最近 N 条日志。"""

    def __init__(self, max_size: int = _MAX_BUFFER):
        self._buf: list[dict[str, Any]] = []
        self._max = max_size
        self._lock = threading.Lock()

    def append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._buf.append(entry)
            if len(self._buf) > self._max:
                self._buf = self._buf[-self._max:]

    def snapshot(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._buf)


_ring = _LogRingBuffer()
_subscribers: list[queue.Queue] = []
_sub_lock = threading.Lock()


class _BroadcastHandler(logging.Handler):
    """将日志同时写入环形缓冲和广播给 SSE 订阅者。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                'ts': _time.strftime('%H:%M:%S', _time.localtime(record.created)),
                'ms': int((record.created % 1) * 1000),
                'level': record.levelname,
                'name': record.name.split('.')[-1][:12],  # 简短模块名
                'msg': self.format(record),
            }
        except Exception:
            return

        # 写入环形缓冲（同步，极小开销）
        _ring.append(entry)

        # 广播给订阅者（非阻塞，队列满则丢弃）
        with _sub_lock:
            subs = list(_subscribers)
        for q in subs:
            try:
                q.put_nowait(entry)
            except queue.Full:
                pass


_handler: _BroadcastHandler | None = None


def start():
    """启动日志广播（在 create_app 时调用一次）。"""
    global _handler
    if _handler:
        return
    _handler = _BroadcastHandler()
    _handler.setFormatter(logging.Formatter('%(message)s'))
    _handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(_handler)
    logger.info('[日志] 实时日志广播已启动')


def subscribe() -> queue.Queue:
    """创建新的日志订阅队列（每个 SSE 连接一个）。"""
    q: queue.Queue = queue.Queue(maxsize=_Q_MAXSIZE)
    with _sub_lock:
        _subscribers.append(q)
        if len(_subscribers) > _MAX_SUBSCRIBERS:
            # 清理断开的连接（超过上限时淘汰最旧的）
            old = _subscribers.pop(0)
            try:
                while old.get_nowait():
                    pass
            except queue.Empty:
                pass
    # 发送初始快照（最近 50 条）
    for entry in _ring.snapshot()[-50:]:
        try:
            q.put_nowait(entry)
        except queue.Full:
            break
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _sub_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def get_recent(n: int = 50) -> list[dict[str, Any]]:
    return _ring.snapshot()[-n:]
