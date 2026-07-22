"""日志分类系统

通过 DEBUG_MODE 环境变量控制日常/调试模式，通过 LOG_CATEGORIES 精细控制。

类别:
  error   — 错误日志（始终开启）
  proxy   — 转发请求/响应摘要（每个 turn 一行）
  admin   — 管理面板 API 调用
  patch   — 补丁引擎匹配/重试
  db      — 数据库操作
  stream  — SSE 流式 chunk 级别调试

用法:
  from utils.log_categories import log, is_enabled

  log('proxy', '请求: model=%s stream=%s msg_count=%d', model, stream, n)
  if is_enabled('stream'):
      # 昂贵的调试日志
      ...
"""

from __future__ import annotations

import logging
import os

# 所有可用类别
ALL_CATEGORIES = {'error', 'proxy', 'admin', 'patch', 'db', 'stream'}

# error 始终开启
_ALWAYS_ON = {'error'}

# 默认配置：每个 DEBUG_MODE 对应的类别
_DEFAULTS: dict[str, set[str]] = {
    'off': {'error'},
    'simple': {'error', 'proxy', 'patch', 'admin', 'db'},
    'verbose': {'error', 'proxy', 'patch', 'admin', 'db', 'stream'},
}

# 当前启用的类别集合
_enabled: set[str] = set()


def init() -> None:
    """根据环境变量初始化启用的日志类别。"""
    global _enabled

    env_cats = os.getenv('LOG_CATEGORIES', '').strip()
    debug_mode = os.getenv('DEBUG_MODE', 'off').strip().lower()

    if env_cats:
        _enabled = _parse_categories(env_cats)
    elif debug_mode in _DEFAULTS:
        _enabled = _DEFAULTS[debug_mode].copy()
    else:
        _enabled = {'error'}

    _enabled |= _ALWAYS_ON
    logging.getLogger(__name__).info(
        '日志类别初始化: DEBUG_MODE=%s enabled=%s', debug_mode, sorted(_enabled)
    )


def is_enabled(category: str) -> bool:
    """检查指定类别是否启用。"""
    return category in _enabled


def log(category: str, msg: str, *args: object, level: int = logging.INFO) -> None:
    """分类别记录日志。category 未启用时直接跳过。"""
    if category not in _enabled:
        return
    logger = logging.getLogger(category)
    logger.log(level, msg, *args)


def _parse_categories(raw: str) -> set[str]:
    """解析逗号/空格分隔的类别字符串。

    支持:
      all        — 全部开启
      error,proxy,stream  — 指定列表
      proxy,-stream       — 开启 proxy，排除 stream
    """
    raw = raw.strip().lower()
    if not raw:
        return set()
    enabled: set[str] = set()
    for part in raw.replace(',', ' ').split():
        part = part.strip()
        if not part:
            continue
        if part == 'all':
            return ALL_CATEGORIES.copy()
        if part in ALL_CATEGORIES:
            enabled.add(part)
        elif part.startswith('-'):
            # 排除某个类别
            excluded = part[1:]
            if excluded in ALL_CATEGORIES:
                enabled.discard(excluded)
    return enabled
