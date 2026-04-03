"""模型定价配置（JSON 文件）

用于用量统计中的费用估算。价格单位为「每 1M tokens」的标价。

支持两种结构（二选一，优先 providers）：
  - **推荐** `providers`：公司 → 系列 → 模型，便于管理面板树形展示；每条模型可有独立 `source_url`。
  - **旧版** `models`：扁平对象，键为 API 模型 id。

用量统计按 Cursor 的 client_model 匹配；可用 `aliases` 映射到模型 `id`。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from config import Config

logger = logging.getLogger(__name__)

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache_doc: dict[str, Any] | None = None
_cache_mtime: float | None = None
_cache_path: str | None = None


def _resolve_path() -> str:
    raw = (getattr(Config, 'MODEL_PRICING_PATH', None) or '').strip()
    if raw:
        return raw if os.path.isabs(raw) else os.path.join(_ROOT_DIR, raw)
    return os.path.join(_ROOT_DIR, 'model_pricing.json')


def invalidate_cache() -> None:
    global _cache_doc, _cache_mtime, _cache_path
    _cache_doc = None
    _cache_mtime = None
    _cache_path = None


def _validate_and_normalize(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """校验结构；返回 (normalized_doc, error)。"""
    providers = raw.get('providers')
    models = raw.get('models')

    if providers is not None and not isinstance(providers, list):
        return None, 'providers 必须是数组'
    if models is not None and not isinstance(models, dict):
        return None, 'models 必须是对象'

    if isinstance(providers, list):
        for pi, p in enumerate(providers):
            if not isinstance(p, dict):
                return None, f'providers[{pi}] 必须是对象'
            series_list = p.get('series')
            if series_list is None:
                continue
            if not isinstance(series_list, list):
                return None, f'providers[{pi}].series 必须是数组'
            for si, ser in enumerate(series_list):
                if not isinstance(ser, dict):
                    return None, f'providers[{pi}].series[{si}] 必须是对象'
                mlist = ser.get('models')
                if mlist is None:
                    continue
                if not isinstance(mlist, list):
                    return None, f'providers[{pi}].series[{si}].models 必须是数组'
                for mi, m in enumerate(mlist):
                    if not isinstance(m, dict):
                        return None, 'series 下 models 项必须是对象'
                    mid = str(m.get('id') or '').strip()
                    if not mid:
                        return None, f'模型缺少 id（providers[{pi}].series[{si}].models[{mi}]）'

    return raw, None


def get_models_flat(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """由文档得到 id → 模型行（含 input/output/source_url 等），供匹配与计价。"""
    providers = doc.get('providers')
    if isinstance(providers, list) and len(providers) > 0:
        flat: dict[str, dict[str, Any]] = {}
        for p in providers:
            if not isinstance(p, dict):
                continue
            for ser in p.get('series') or []:
                if not isinstance(ser, dict):
                    continue
                for m in ser.get('models') or []:
                    if not isinstance(m, dict):
                        continue
                    mid = str(m.get('id') or '').strip()
                    if not mid:
                        continue
                    if mid in flat:
                        logger.warning('model_pricing: 重复模型 id %r，后出现的条目覆盖前者', mid)
                    flat[mid] = m
        return flat

    m = doc.get('models')
    if isinstance(m, dict):
        return dict(m)
    return {}


def load_document() -> tuple[dict[str, Any], dict[str, Any]]:
    """读取定价 JSON；带 mtime 缓存。返回 (document, meta)。"""
    global _cache_doc, _cache_mtime, _cache_path
    path = _resolve_path()
    meta: dict[str, Any] = {
        'path': path,
        'loaded': False,
        'error': None,
        'mtime': None,
    }
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        invalidate_cache()
        meta['error'] = f'无法读取定价文件: {e}'
        doc = _empty_doc()
        return doc, meta

    meta['mtime'] = mtime
    if (
        _cache_doc is not None
        and _cache_mtime == mtime
        and _cache_path == path
    ):
        meta['loaded'] = True
        return _cache_doc, meta

    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning('model_pricing: 解析失败 %s', e)
        invalidate_cache()
        meta['error'] = str(e)
        doc = _empty_doc()
        _cache_doc = doc
        _cache_mtime = mtime
        _cache_path = path
        return doc, meta

    if not isinstance(raw, dict):
        invalidate_cache()
        meta['error'] = '根节点必须是 JSON 对象'
        doc = _empty_doc()
        _cache_doc = doc
        _cache_mtime = mtime
        _cache_path = path
        return doc, meta

    norm, err = _validate_and_normalize(raw)
    if err:
        invalidate_cache()
        meta['error'] = err
        doc = _empty_doc()
        _cache_doc = doc
        _cache_mtime = mtime
        _cache_path = path
        return doc, meta

    _cache_doc = norm
    _cache_mtime = mtime
    _cache_path = path
    meta['loaded'] = True
    return norm, meta


def _empty_doc() -> dict[str, Any]:
    return {
        'schema_version': 2,
        'currency': 'CNY',
        'currency_symbol': '¥',
        'note': '',
        'updated_at': '',
        'providers': [],
        'models': {},
        'aliases': {},
    }


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x < 0:
        return None
    return x


def resolve_row(client_model: str, doc: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str]:
    """返回 (定价行, canonical 模型 id, 匹配方式 exact|alias|none)。"""
    models = get_models_flat(doc)

    if client_model in models and isinstance(models[client_model], dict):
        return models[client_model], client_model, 'exact'

    aliases = doc.get('aliases') or {}
    if isinstance(aliases, dict) and client_model in aliases:
        canon = str(aliases[client_model] or '').strip()
        if canon and canon in models and isinstance(models[canon], dict):
            return models[canon], canon, 'alias'

    return None, None, 'none'


def estimate_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    row: dict[str, Any] | None,
) -> dict[str, Any]:
    """根据单行定价估算费用（标价 × tokens / 1e6）。"""
    if not row:
        return {
            'priced': False,
            'estimated_cost': None,
            'input_price_per_million': None,
            'output_price_per_million': None,
        }
    pin = _num(row.get('input_per_million'))
    pout = _num(row.get('output_per_million'))
    if pin is None and pout is None:
        return {
            'priced': False,
            'estimated_cost': None,
            'input_price_per_million': pin,
            'output_price_per_million': pout,
        }
    pin = pin or 0.0
    pout = pout or 0.0
    cost = (max(0, input_tokens) / 1_000_000.0) * pin + (max(0, output_tokens) / 1_000_000.0) * pout
    return {
        'priced': True,
        'estimated_cost': round(cost, 8),
        'input_price_per_million': pin,
        'output_price_per_million': pout,
    }


def enrich_usage_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """在 get_stats 结果上附加定价与预估费用。"""
    doc, meta = load_document()
    currency = str(doc.get('currency') or '')
    symbol = str(doc.get('currency_symbol') or '')
    models_in = stats.get('models') or {}
    if not isinstance(models_in, dict):
        return stats

    out_models: dict[str, Any] = {}
    total = 0.0
    any_priced = False

    for name, s in models_in.items():
        if not isinstance(s, dict):
            out_models[name] = s
            continue
        row, canon, match = resolve_row(str(name), doc)
        est = estimate_cost(
            input_tokens=int(s.get('input_tokens') or 0),
            output_tokens=int(s.get('output_tokens') or 0),
            row=row,
        )
        if est.get('priced') and est.get('estimated_cost') is not None:
            total += float(est['estimated_cost'])
            any_priced = True
        src = ''
        if isinstance(row, dict):
            src = str(row.get('source_url') or '').strip()
        out_models[name] = {
            **s,
            **est,
            'pricing_match': match,
            'pricing_model_key': canon,
            'pricing_source_url': src,
        }

    pricing_summary = {
        'currency': currency,
        'currency_symbol': symbol,
        'updated_at': str(doc.get('updated_at') or ''),
        'note': str(doc.get('note') or ''),
        'file_path': meta.get('path', ''),
        'file_loaded': bool(meta.get('loaded')),
        'file_error': meta.get('error'),
    }

    return {
        **stats,
        'models': out_models,
        'pricing': pricing_summary,
        'estimated_total_cost': round(total, 6) if any_priced else None,
    }


def snapshot_for_admin() -> dict[str, Any]:
    """管理接口：完整文档 + 元信息。"""
    doc, meta = load_document()
    return {
        'document': doc,
        'meta': {
            'path': meta.get('path'),
            'mtime': meta.get('mtime'),
            'loaded': meta.get('loaded'),
            'error': meta.get('error'),
        },
    }
