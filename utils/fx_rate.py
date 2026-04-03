"""USD→CNY 汇率获取与缓存。

- 首选内存缓存，其次本地 data/fx_rate.json，本次进程至少有一个近似值。
- 如配置 MXNZP_APP_ID / MXNZP_APP_SECRET，则在缓存过期或无缓存时调用：
  https://www.mxnzp.com/api/exchange_rate/list?app_id=...&app_secret=...
- 只提取 USD/CNY 一条；失败时不抛异常，而是返回 (None, meta)。

仅用于费用估算，实际结算以支付渠道为准。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Tuple
from urllib import parse, request

import settings

logger = logging.getLogger(__name__)

_DATA_FILE = os.path.join(settings.DATA_DIR, "fx_rate.json")
_CACHE_TTL_SECONDS = 6 * 3600  # 6 小时

__cache_rate: float | None = None
__cache_ts: float | None = None
__cache_meta: dict[str, Any] | None = None


def _env_disabled() -> bool:
    return os.getenv("FX_RATE_DISABLED", "").lower() in ("1", "true", "yes", "on")


def _api_credentials() -> tuple[str | None, str | None, str]:
    s = settings.get()
    app_id_cfg = str(s.get("mxnzp_app_id") or "").strip()
    app_secret_cfg = str(s.get("mxnzp_app_secret") or "").strip()
    app_id_env = os.getenv("MXNZP_APP_ID", "").strip()
    app_secret_env = os.getenv("MXNZP_APP_SECRET", "").strip()
    app_id = app_id_cfg or app_id_env
    app_secret = app_secret_cfg or app_secret_env
    url = os.getenv("FX_RATE_API_URL", "https://www.mxnzp.com/api/exchange_rate/list").strip()
    return (app_id or None), (app_secret or None), url


def _load_disk() -> tuple[float | None, dict[str, Any]]:
    try:
        if not os.path.exists(_DATA_FILE):
            return None, {"source": "disk", "reason": "no_file"}
        with open(_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        rate = float(data.get("usd_cny")) if data.get("usd_cny") is not None else None
        meta = {
            "source": "disk",
            "updated_at": data.get("updated_at"),
            "api_url": data.get("api_url"),
            "note": data.get("note"),
        }
        return rate, meta
    except Exception as e:  # noqa: BLE001
        logger.warning("fx_rate: 读取本地缓存失败 %s", e)
        return None, {"source": "disk", "reason": "read_error", "error": str(e)}


def _save_disk(rate: float, meta: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(_DATA_FILE), exist_ok=True)
        payload = {
            "usd_cny": rate,
            "updated_at": meta.get("updated_at"),
            "api_url": meta.get("api_url"),
            "note": meta.get("note"),
        }
        with open(_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        logger.warning("fx_rate: 写入本地缓存失败 %s", e)


def _call_api() -> tuple[float | None, dict[str, Any]]:
    app_id, app_secret, base_url = _api_credentials()
    if not app_id or not app_secret:
        return None, {"source": "remote", "reason": "missing_credentials", "api_url": base_url}
    try:
        qs = parse.urlencode({"app_id": app_id, "app_secret": app_secret})
        url = base_url + ("?" + qs if "?" not in base_url else "&" + qs)
        req = request.Request(url, method="GET")
        with request.urlopen(req, timeout=5) as resp:  # noqa: S310
            body = resp.read()
        data = json.loads(body.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        logger.warning("fx_rate: 远程获取失败 %s", e)
        return None, {"source": "remote", "reason": "request_failed", "error": str(e), "api_url": base_url}

    try:
        if int(data.get("code")) != 1:
            return None, {"source": "remote", "reason": "bad_code", "raw": data, "api_url": base_url}
        items = data.get("data") or []
        rate_val: float | None = None
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("from") == "USD" and item.get("to") == "CNY":
                price = item.get("price")
                try:
                    rate_val = float(price)
                    break
                except (TypeError, ValueError):
                    continue
        if rate_val is None:
            return None, {"source": "remote", "reason": "no_usd_cny", "raw": data, "api_url": base_url}
        meta = {
            "source": "remote",
            "api_url": base_url,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "note": "from MXNZP exchange_rate list",
        }
        _save_disk(rate_val, meta)
        return rate_val, meta
    except Exception as e:  # noqa: BLE001
        logger.warning("fx_rate: 解析响应失败 %s", e)
        return None, {"source": "remote", "reason": "parse_error", "error": str(e), "api_url": base_url}


def get_usd_cny_rate() -> Tuple[float | None, dict[str, Any]]:
    """返回 (rate, meta)。rate 为 1 USD → CNY，失败时为 None。"""
    global __cache_rate, __cache_ts, __cache_meta

    if _env_disabled():
        rate, meta = _load_disk()
        meta.setdefault("source", "disk")
        meta.setdefault("disabled", True)
        return rate, meta

    now = time.time()
    if __cache_rate is not None and __cache_ts is not None and (now - __cache_ts) < _CACHE_TTL_SECONDS:
        meta = dict(__cache_meta or {})
        meta.setdefault("source", "cache")
        return __cache_rate, meta

    rate, meta = _call_api()
    if rate is None:
        disk_rate, disk_meta = _load_disk()
        if disk_rate is not None:
            __cache_rate = disk_rate
            __cache_ts = now
            __cache_meta = disk_meta
            out_meta = dict(disk_meta)
            out_meta.setdefault("source", "disk")
            return disk_rate, out_meta
        return None, meta

    __cache_rate = rate
    __cache_ts = now
    __cache_meta = meta
    meta2 = dict(meta)
    meta2.setdefault("source", "remote")
    return rate, meta2

