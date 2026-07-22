"""路由: 管理面板

提供 Web 管理界面和 API：
  - /admin         — 管理面板页面
  - /v1/models     — 模型列表（供 Cursor 查询）
  - /api/admin/*   — 登录验证、全局设置 CRUD、模型映射 CRUD
"""

import os
import io
import logging
import json
import glob
import queue
import sys
import time
import threading
import zipfile
from datetime import datetime, timezone
from typing import Any

from flask import Blueprint, request, jsonify, send_from_directory, send_file, Response, stream_with_context

import settings
from config import Config
from settings import DATA_DIR
from utils.http import sse_response
from routes.common import sse_data_message
from utils import request_logger as request_logger_mod
from utils import conversation_index as conv_idx
from utils import conversation_store

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')

bp = Blueprint('admin', __name__)


# ─── 静态页面 ─────────────────────────────────────


@bp.route('/admin')
@bp.route('/admin/')
def admin_page():
    """返回管理面板首页 HTML 页面，供浏览器进入配置界面。"""
    return send_from_directory(_STATIC_DIR, 'admin.html')


@bp.route('/admin/conversations')
@bp.route('/admin/conversations/')
def admin_conversations_page():
    """返回会话回放分析页。"""
    return send_from_directory(_STATIC_DIR, 'conversations.html')


@bp.route('/static/<path:filename>')
def static_files(filename):
    """提供管理面板所需的静态资源文件。"""
    return send_from_directory(_STATIC_DIR, filename)


# ─── 模型列表 ─────────────────────────────────────


@bp.route('/v1/models', methods=['GET'])
def list_models():
    """返回当前配置的模型列表，供 Cursor 拉取可用模型。"""
    mappings = settings.get().get('model_mappings', {})
    models = [{
        'id': name,
        'object': 'model',
        'owned_by': info.get('backend', 'custom'),
    } for name, info in mappings.items()]

    if not models:
        models.append({
            'id': 'claude-sonnet-4-5-20250929',
            'object': 'model',
            'owned_by': 'anthropic',
        })
    return jsonify({'object': 'list', 'data': models})


# ─── 登录验证 ─────────────────────────────────────


@bp.route('/api/admin/login', methods=['POST'])
def admin_login():
    """校验管理面板登录密钥，并返回是否允许进入后台。"""
    data = request.get_json(force=True)
    if not Config.ACCESS_API_KEY:
        return jsonify({'ok': True, 'message': '未配置鉴权'})
    if data.get('key', '') == Config.ACCESS_API_KEY:
        return jsonify({'ok': True})
    return jsonify({'ok': False, 'message': '密钥错误'}), 401


# ─── 全局设置 ─────────────────────────────────────


@bp.route('/api/admin/settings', methods=['GET'])
def get_settings():
    """读取当前生效的全局代理配置。"""
    err = _check_auth()
    if err:
        return err
    s = settings.get()
    return jsonify({
        'proxy_target_url': s.get('proxy_target_url', ''),
        'proxy_api_key': s.get('proxy_api_key', ''),
        'debug_mode': s.get('debug_mode', '') or Config.DEBUG_MODE,
        'mxnzp_app_id': s.get('mxnzp_app_id', ''),
        'mxnzp_app_secret': s.get('mxnzp_app_secret', ''),
        'fx_rate_api_url': s.get('fx_rate_api_url', ''),
        'env_target_url': Config.PROXY_TARGET_URL,
        'env_api_key': '***' if Config.PROXY_API_KEY else '',
    })


@bp.route('/api/admin/settings', methods=['PUT'])
def update_settings():
    """更新全局上游地址与密钥配置。"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True)
    s = settings.get()
    for key in ('proxy_target_url', 'proxy_api_key', 'debug_mode', 'mxnzp_app_id', 'mxnzp_app_secret', 'fx_rate_api_url'):
        if key in data:
            s[key] = data[key]
    return _save_and_respond(s, '全局设置已更新')


# ─── 模型映射 CRUD ────────────────────────────────


@bp.route('/api/admin/mappings', methods=['GET'])
def list_mappings():
    """列出所有模型映射配置，供管理面板读取和展示。"""
    err = _check_auth()
    if err:
        return err
    return jsonify(settings.get().get('model_mappings', {}))


@bp.route('/api/admin/mappings', methods=['POST'])
def add_mapping():
    """新增一条模型映射，并写入持久化配置。"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': '名称不能为空'}), 400

    s = settings.get()
    mappings = s.setdefault('model_mappings', {})
    mappings[name] = {
        'upstream_model': data.get('upstream_model', name),
        'backend': data.get('backend', 'auto'),
        'target_url': data.get('target_url', ''),
        'api_key': data.get('api_key', ''),
        'custom_instructions': data.get('custom_instructions', ''),
        'instructions_position': data.get('instructions_position', 'prepend'),
        'body_modifications': data.get('body_modifications') or {},
        'header_modifications': data.get('header_modifications') or {},
    }
    return _save_and_respond(s, f'映射已添加: {name}')


@bp.route('/api/admin/mappings/<path:name>', methods=['PUT'])
def update_mapping(name):
    """更新指定名称的模型映射，必要时支持重命名。"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True)
    s = settings.get()
    mappings = s.get('model_mappings', {})
    if name not in mappings:
        return jsonify({'error': '映射不存在'}), 404

    new_name = data.get('name', name).strip()
    entry = {
        'upstream_model': data.get('upstream_model', name),
        'backend': data.get('backend', 'auto'),
        'target_url': data.get('target_url', ''),
        'api_key': data.get('api_key', ''),
        'custom_instructions': data.get('custom_instructions', ''),
        'instructions_position': data.get('instructions_position', 'prepend'),
        'body_modifications': data.get('body_modifications') or {},
        'header_modifications': data.get('header_modifications') or {},
    }
    if new_name != name:
        del mappings[name]
    mappings[new_name] = entry
    s['model_mappings'] = mappings
    return _save_and_respond(s, f'映射已更新: {name} → {new_name}')


@bp.route('/api/admin/mappings/<path:name>', methods=['DELETE'])
def delete_mapping(name):
    """删除指定名称的模型映射，并在存在时同步保存配置。"""
    err = _check_auth()
    if err:
        return err
    s = settings.get()
    mappings = s.get('model_mappings', {})
    if name in mappings:
        del mappings[name]
        s['model_mappings'] = mappings
        return _save_and_respond(s, f'映射已删除: {name}')
    return jsonify({'ok': True})


# ─── 用量统计 ─────────────────────────────────────


@bp.route('/api/admin/stats', methods=['GET'])
def get_stats():
    """返回运行时用量统计数据。"""
    err = _check_auth()
    if err:
        return err
    from utils.usage_tracker import usage_tracker
    from utils.model_pricing import enrich_usage_stats
    from utils.fx_rate import get_usd_cny_rate

    stats = enrich_usage_stats(usage_tracker.get_stats())
    rate, meta = get_usd_cny_rate()
    stats['fx'] = {
        'usd_cny': rate,
        'source': meta.get('source'),
        'updated_at': meta.get('updated_at'),
        'api_url': meta.get('api_url'),
        'note': meta.get('note'),
        'disabled': bool(meta.get('disabled')),
    }
    return jsonify(stats)


@bp.route('/api/admin/pricing', methods=['GET'])
def get_model_pricing():
    """返回当前定价 JSON 全文（供管理面板查看价格表）。"""
    err = _check_auth()
    if err:
        return err
    from utils.model_pricing import snapshot_for_admin

    return jsonify(snapshot_for_admin())


@bp.route('/api/admin/pricing/reload', methods=['POST'])
def reload_model_pricing():
    """清除定价文件缓存，下次读取时重新加载磁盘文件。"""
    err = _check_auth()
    if err:
        return err
    from utils.model_pricing import invalidate_cache, load_document

    invalidate_cache()
    _, meta = load_document()
    return jsonify({'ok': True, 'meta': meta})


@bp.route('/api/admin/fx-rate', methods=['GET'])
def get_fx_rate():
    """单独查询一次 USD→CNY 汇率（供前端手动刷新）。"""
    err = _check_auth()
    if err:
        return err
    from utils.fx_rate import get_usd_cny_rate

    rate, meta = get_usd_cny_rate()
    return jsonify({
        'usd_cny': rate,
        'source': meta.get('source'),
        'updated_at': meta.get('updated_at'),
        'api_url': meta.get('api_url'),
        'note': meta.get('note') or '仅供估算，实际以支付渠道汇率为准。',
        'disabled': bool(meta.get('disabled')),
    })


# ─── 配置导入 / 导出 ──────────────────────────────────


@bp.route('/api/admin/config/export', methods=['GET'])
def export_config():
    """导出当前可配置项（settings.json 的全部内容）。"""
    err = _check_auth()
    if err:
        return err
    s = settings.get()
    return jsonify({
        'type': 'api2cursor_config',
        'version': 1,
        'exported_at': s.get('updated_at', ''),
        'settings': s,
    })


@bp.route('/api/admin/config/import', methods=['POST'])
def import_config():
    """导入配置（覆盖写入 settings.json）。"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    payload = data.get('settings') if isinstance(data, dict) and 'settings' in data else data
    if not isinstance(payload, dict):
        return jsonify({'error': {'message': '配置必须为 JSON 对象', 'type': 'bad_request'}}), 400

    allowed_top = {
        'proxy_target_url',
        'proxy_api_key',
        'debug_mode',
        'model_mappings',
        'mxnzp_app_id',
        'mxnzp_app_secret',
        'fx_rate_api_url',
    }
    cleaned: dict[str, Any] = {}
    for k in allowed_top:
        if k in payload:
            cleaned[k] = payload[k]

    # model_mappings 基础校验
    mappings = cleaned.get('model_mappings', {})
    if mappings is not None and not isinstance(mappings, dict):
        return jsonify({'error': {'message': 'model_mappings 必须是对象', 'type': 'bad_request'}}), 400

    try:
        settings.save(cleaned)
    except OSError as e:
        logger.error(f'导入配置保存失败: {e}')
        return jsonify({'error': {'message': f'保存失败: {e}', 'type': 'save_error'}}), 500

    logger.info('配置已通过导入覆盖更新')
    return jsonify({'ok': True})


# ─── 内部辅助 ─────────────────────────────────────


def _check_auth():
    """Admin API 鉴权，返回 None 表示通过"""
    if not Config.ACCESS_API_KEY:
        return None
    auth = request.headers.get('Authorization', '')
    token = auth[7:] if auth.startswith('Bearer ') else request.headers.get('x-api-key', '')
    if token != Config.ACCESS_API_KEY:
        return jsonify({'error': '未授权'}), 401
    return None


def _save_and_respond(data, log_msg):
    """保存配置并返回统一成功响应。

    当写盘失败时，这里也负责把异常转成结构化的 JSON 错误返回。
    """
    try:
        settings.save(data)
    except OSError as e:
        logger.error(f'保存失败: {e}')
        return jsonify({'error': {'message': f'保存失败: {e}', 'type': 'save_error'}}), 500
    logger.info(log_msg)
    return jsonify({'ok': True})


# ─── 实时日志 / 请求响应日志 ─────────────────────────────

_LOG_DIR = os.path.join(DATA_DIR, 'conversations')
_NOTES_FILE = os.path.join(DATA_DIR, 'log_notes.json')
_NOTES_LOCK = threading.Lock()


def _load_log_notes() -> dict[str, Any]:
    with _NOTES_LOCK:
        if not os.path.exists(_NOTES_FILE):
            return {}
        try:
            with open(_NOTES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def _save_log_notes(notes: dict[str, Any]) -> None:
    with _NOTES_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_NOTES_FILE, 'w', encoding='utf-8') as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)


# ─── SSE 临时 token（避免主密钥出现在 URL 中）──────────────────
_SSE_TOKENS: dict[str, float] = {}  # token → expiry_timestamp
_SSE_TOKENS_LOCK = threading.Lock()
_SSE_TOKEN_TTL = 300  # 5 分钟


def _generate_sse_token() -> str:
    """生成一个有时效的临时 token，用于 SSE 连接鉴权。"""
    import secrets
    tok = 'sse_' + secrets.token_urlsafe(24)
    with _SSE_TOKENS_LOCK:
        _SSE_TOKENS[tok] = time.time() + _SSE_TOKEN_TTL
        # 清理过期 token
        now = time.time()
        expired = [k for k, v in _SSE_TOKENS.items() if v < now]
        for k in expired:
            del _SSE_TOKENS[k]
    return tok


def _validate_sse_token(token: str) -> bool:
    """验证临时 SSE token 是否有效。"""
    with _SSE_TOKENS_LOCK:
        expiry = _SSE_TOKENS.get(token, 0)
        if expiry > time.time():
            return True
        _SSE_TOKENS.pop(token, None)
        return False


def _check_auth_with_query_key() -> Any | None:
    """Admin API 鉴权：优先支持 query key（用于 EventSource 无自定义 header 场景）。

    支持两种方式：
    1. 主密钥（Authorization header / x-api-key header / ?key= 参数）
    2. 临时 SSE token（仅 ?key= 参数，5 分钟有效，URL 泄露后影响有限）
    """
    if not Config.ACCESS_API_KEY:
        return None
    token = request.args.get('key', '') or request.headers.get('Authorization', '')
    if isinstance(token, str) and token.startswith('Bearer '):
        token = token[7:]
    if not token:
        token = request.headers.get('x-api-key', '')

    # 优先匹配主密钥
    if token == Config.ACCESS_API_KEY:
        return None
    # 其次匹配临时 SSE token（仅对 SSE 接口有效）
    if token.startswith('sse_') and _validate_sse_token(token):
        return None
    return jsonify({'error': '未授权'}), 401


def _find_conversation_file(conversation_id: str, date: str | None = None) -> str | None:
    ap = conv_idx.resolve_abs_path(conversation_id, date)
    if ap:
        return ap
    if not os.path.isdir(_LOG_DIR):
        return None
    if date:
        p = os.path.join(_LOG_DIR, date, f'{conversation_id}.json')
        return p if os.path.exists(p) else None

    pattern = os.path.join(_LOG_DIR, '*', f'{conversation_id}.json')
    matches = glob.glob(pattern)
    if not matches:
        return None
    matches.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return matches[0]


def _list_conversation_files() -> list[str]:
    if not os.path.isdir(_LOG_DIR):
        return []
    pattern = os.path.join(_LOG_DIR, '*', '*.json')
    files = glob.glob(pattern)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    return files


def _paths_for_export_all() -> list[str]:
    """优先用索引得到路径；索引空而磁盘有文件时重建一次；仍不可用则回退 glob。"""
    rels = conv_idx.list_all_rel_paths()
    if rels is None:
        return _list_conversation_files()

    def collect(rlist: list[str]) -> list[str]:
        out: list[str] = []
        for rel in rlist:
            ap = conv_idx.abs_path_from_rel(rel)
            if os.path.isfile(ap):
                out.append(ap)
        return out

    paths = collect(rels)
    if not paths and os.path.isdir(_LOG_DIR):
        pattern = os.path.join(_LOG_DIR, '*', '*.json')
        if glob.glob(pattern):
            conv_idx.rebuild_from_disk()
            paths = collect(conv_idx.list_all_rel_paths() or [])
    if not paths:
        return _list_conversation_files()
    return paths


def _dt_to_index_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _parse_iso_dt(s: str) -> datetime | None:
    """解析 ISO8601 时间（支持 Z 结尾）；无时区则按 UTC。"""
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


def _dt_in_range(t: datetime, start: datetime | None, end: datetime | None) -> bool:
    if start is not None and t < start:
        return False
    if end is not None and t > end:
        return False
    return True


def _conversation_doc_in_time_range(doc: dict[str, Any], start: datetime | None, end: datetime | None) -> bool:
    """判断会话文档是否与 [start, end]（含端点）有交集。"""
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
    if not times:
        return False
    return any(_dt_in_range(t, start, end) for t in times)


def _conversation_has_recorded_error(doc: dict[str, Any]) -> bool:
    """会话内是否至少有一条 turn 带有 error（代理写入的上游/转发错误等）。"""
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


def _pick_last_suspect_export_files() -> tuple[list[str], str]:
    """按文件修改时间从新到旧扫描，优先选「含 turn.error」的最近一条；否则取最新一条。

    返回 (paths, reason_code)。
    """
    files = _list_conversation_files()
    if not files:
        return [], 'no_files'

    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and _conversation_has_recorded_error(doc):
            return [fp], 'has_turn_error'

    return [files[0]], 'newest_fallback'


def _logs_export_readme() -> str:
    return (
        'api2cursor 日志导出包说明\n'
        '========================\n\n'
        '01_cursor_proxy_sessions/\n'
        '  按日期分目录的会话 JSON（与服务器 data/conversations 下内容一致，二进制原样打包）。\n'
        '  每条 turn 含 client_request / upstream_request / upstream_response / client_response / stream_trace 等，\n'
        '  用于分析 Cursor → 本代理 → 上游 LLM 的交互流程。\n\n'
        '02_application_meta/\n'
        '  settings_snapshot.json — 当前持久化配置快照（data/settings.json 等价）。\n'
        '  log_notes.json — 管理面板为会话添加的备注。\n\n'
        'manifest.json — 导出元数据（时间、筛选条件、文件数量等）。\n\n'
        '关于「流式是否截断」：若环境变量 VERBOSE_FULL_STREAM=1，则 verbose 模式下写入磁盘的\n'
        'stream_trace 事件为完整列表；否则可能仅保留头尾若干条（中间折叠计数），详见 request_logger。\n\n'
        '导出模式「最近可疑会话」：manifest.export_mode=last_suspect 时仅含 1 个会话文件；\n'
        '优先选最近修改且含 turn.error 的会话；若无则取最新一条（last_suspect_pick_reason=newest_fallback）。\n'
        'has_turn_error = 命中含错误的 turn；newest_fallback = 历史里暂无 error 字段时仍导出最新会话。\n'
    )


@bp.route('/api/admin/logs/export', methods=['POST'])
def logs_export_zip():
    """导出会话日志等为 ZIP（默认全部，或按时间范围）。"""
    err = _check_auth()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    export_all = bool(data.get('all', False))
    last_suspect = bool(data.get('last_suspect') or data.get('lastSuspect'))
    start_s = (data.get('start') or '').strip()
    end_s = (data.get('end') or '').strip()

    start_dt = _parse_iso_dt(start_s) if start_s else None
    end_dt = _parse_iso_dt(end_s) if end_s else None

    if last_suspect:
        if export_all or start_s or end_s:
            return jsonify(
                {
                    'error': {
                        'message': 'last_suspect 与 all / 时间范围互斥，请勿同时传',
                        'type': 'bad_request',
                    }
                }
            ), 400
        included = []
        suspect_reason = ''
        rel, idx_reason = conv_idx.pick_last_suspect_rel_path()
        if rel and idx_reason in ('has_turn_error', 'newest_fallback'):
            ap = conv_idx.abs_path_from_rel(rel)
            if os.path.isfile(ap):
                included = [ap]
                suspect_reason = idx_reason
        if not included:
            included, suspect_reason = _pick_last_suspect_export_files()
        if not included:
            return jsonify(
                {
                    'error': {
                        'message': '没有可导出的会话日志（data/conversations 下无 json）',
                        'type': 'bad_request',
                    }
                }
            ), 400
    elif not export_all:
        if start_dt is None or end_dt is None:
            return jsonify({'error': {'message': '请设置 all=true，或同时提供 start 与 end（ISO8601）', 'type': 'bad_request'}}), 400
        if start_dt > end_dt:
            return jsonify({'error': {'message': 'start 不能晚于 end', 'type': 'bad_request'}}), 400

        start_iso = _dt_to_index_iso(start_dt)
        end_iso = _dt_to_index_iso(end_dt)
        rels = conv_idx.list_rel_paths_time_range_overlap(start_iso, end_iso)
        included = []
        if rels is not None:
            for rel in rels:
                ap = conv_idx.abs_path_from_rel(rel)
                if os.path.isfile(ap):
                    included.append(ap)
        else:
            for fp in _list_conversation_files():
                try:
                    with open(fp, 'r', encoding='utf-8') as f:
                        doc = json.load(f)
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(doc, dict) and _conversation_doc_in_time_range(doc, start_dt, end_dt):
                    included.append(fp)
        suspect_reason = ''
    else:
        included = _paths_for_export_all()
        suspect_reason = ''

    buf = io.BytesIO()
    exported_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest: dict[str, Any] = {
            'type': 'api2cursor_logs_export',
            'version': 1,
            'exported_at': exported_at,
            'export_all': export_all,
            'export_mode': 'last_suspect' if last_suspect else ('all' if export_all else 'time_range'),
            'last_suspect_pick_reason': suspect_reason if last_suspect else None,
            'time_range': None if export_all or last_suspect else {'start': start_s, 'end': end_s},
            'session_file_count': len(included),
            'verbose_full_stream_env': os.getenv('VERBOSE_FULL_STREAM', ''),
        }
        zf.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr('02_application_meta/README.txt', _logs_export_readme())

        try:
            snap = settings.get()
            zf.writestr(
                '02_application_meta/settings_snapshot.json',
                json.dumps(snap, ensure_ascii=False, indent=2, default=str),
            )
        except Exception as e:
            zf.writestr('02_application_meta/settings_snapshot.error.txt', str(e))

        notes = _load_log_notes()
        zf.writestr(
            '02_application_meta/log_notes.json',
            json.dumps(notes, ensure_ascii=False, indent=2, default=str),
        )

        for fp in included:
            rel = os.path.relpath(fp, _LOG_DIR).replace('\\', '/')
            arcname = f'01_cursor_proxy_sessions/{rel}'
            with open(fp, 'rb') as f:
                zf.writestr(arcname, f.read())

    buf.seek(0)
    ts = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    fname = f'api2cursor-logs-last-suspect-{ts}.zip' if last_suspect else f'api2cursor-logs-{ts}.zip'
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=fname,
        max_age=0,
    )


@bp.route('/api/admin/sse-token', methods=['POST'])
def get_sse_token():
    """用主密钥换取临时 SSE token（避免主密钥出现在 URL query string 中）。

    POST /api/admin/sse-token
    Authorization: Bearer <主密钥>

    返回: {"token": "sse_xxxxx", "expires_in": 300}
    """
    err = _check_auth()
    if err:
        return err
    tok = _generate_sse_token()
    return jsonify({'token': tok, 'expires_in': _SSE_TOKEN_TTL})


@bp.route('/api/admin/logs/live', methods=['GET'])
def logs_live_sse():
    """实时推送 verbose 模式下的 request/response 日志事件。

    Query 参数:
      ?filter=turn    只推送 turn_started / turn_done（减少 99% 流量，推荐前端使用）
                      不加此参数则推送全部事件（调试用）
    """
    err = _check_auth_with_query_key()
    if err:
        return err

    event_filter = (request.args.get('filter') or '').strip()

    def gen():
        q = request_logger_mod.register_live_subscriber()
        try:
            yield sse_data_message({'type': 'hello', 'message': 'live logs connected'})
            while True:
                try:
                    evt = q.get(timeout=2)
                except queue.Empty:
                    yield sse_data_message({'type': 'ping'})
                    continue
                # ?filter=turn 模式：只推送前端实际需要的 turn 事件
                if event_filter == 'turn':
                    kind = evt.get('kind', '')
                    if kind not in ('turn_started', 'turn_done'):
                        continue
                yield sse_data_message(evt)
        finally:
            request_logger_mod.unregister_live_subscriber(q)

    return sse_response(gen())


@bp.route('/api/admin/logs/count', methods=['GET'])
def logs_count():
    """返回会话数量，优先从数据库读取。"""
    err = _check_auth()
    if err:
        return err
    try:
        n = conversation_store.get_conversation_count()
    except Exception:
        n = conv_idx.count_rows()
        if n is None and os.path.isdir(_LOG_DIR):
            n = len(glob.glob(os.path.join(_LOG_DIR, '*', '*.json')))
    return jsonify({'count': n or 0})


@bp.route('/api/admin/logs', methods=['GET'])
def logs_list():
    """列出最近的会话日志（历史），优先从数据库读取。"""
    import time as _time
    _start = _time.time()
    err = _check_auth()
    if err:
        return err

    limit = int(request.args.get('limit', '30'))
    q = (request.args.get('q') or '').strip()
    date = (request.args.get('date') or '').strip() or None
    sort = (request.args.get('sort') or 'updated_at').strip()
    dir_ = (request.args.get('dir') or 'desc').strip()
    fix_status = (request.args.get('fix_status') or '').strip()

    notes = _load_log_notes()

    _t_db_start = _time.time()
    rows: list[dict[str, Any]] = []
    _db_error: str | None = None
    try:
        rows = conversation_store.list_conversations(
            limit=limit, q=q, date=date or '', sort_field=sort, sort_dir=dir_,
            fix_status=fix_status,
        )
    except Exception as e:
        _db_error = str(e)[:200]
        logger.warning('[性能] GET /api/admin/logs DB 查询异常 (将重试): %s', _db_error)
        # 短暂等待后重试一次（可能只是 WAL 检查点导致的瞬时锁）
        try:
            _time.sleep(0.1)
            rows = conversation_store.list_conversations(
                limit=limit, q=q, date=date or '', sort_field=sort, sort_dir=dir_,
                fix_status=fix_status,
            )
            logger.info('[性能] GET /api/admin/logs DB 重试成功 → %d条', len(rows))
        except Exception as e2:
            _db_error = str(e2)[:200]
            logger.error('[性能] GET /api/admin/logs DB 重试仍失败: %s', _db_error)
    _t_db_ms = (_time.time() - _t_db_start) * 1000

    if rows or _db_error is None:
        # DB 查询成功（即使结果为空也走这里——筛选条件可能确实没匹配到数据）
        _t_build_start = _time.time()
        items: list[dict[str, Any]] = []
        for row in rows:
            cid = row.get('id', '')
            updated = row.get('updated_at', '')
            items.append({
                'conversation_id': cid,
                'date': updated[:10] if updated else '',
                'route': row.get('route', '') or '',
                'last_client_model': row.get('last_client_model', '') or '',
                'last_backend': row.get('last_backend', '') or '',
                'created_at': row.get('created_at', '') or '',
                'updated_at': updated or '',
                'turn_count': int(row.get('turn_count', 0) or 0),
                'error_turn_count': int(row.get('error_turn_count', 0) or 0),
                'has_error': bool(row.get('has_error', 0)),
                'fix_status': row.get('fix_status', '') or '',
                'note': (notes.get(cid) or {}).get('note', ''),
            })
        _t_build_ms = (_time.time() - _t_build_start) * 1000
        _t_json_start = _time.time()
        result = jsonify({'items': items})
        _t_json_ms = (_time.time() - _t_json_start) * 1000
        total_ms = (_time.time() - _start) * 1000
        logger.info(
            '[性能] GET /api/admin/logs limit=%d q=%r date=%r fix_status=%r sort=%s %s '
            '→ %d条 DB=%.0fms build=%.0fms json=%.0fms total=%.0fms',
            limit, q[:30] if q else '', date or '', fix_status, sort, dir_,
            len(items), _t_db_ms, _t_build_ms, _t_json_ms, total_ms,
        )
        if not items and fix_status:
            logger.info('[性能] GET /api/admin/logs DB 查询成功但筛选结果为空 (fix_status=%r)', fix_status)
        return result

    # 回退：DB 查询失败（有异常）时读文件
    logger.warning('[性能] GET /api/admin/logs DB 不可用，回退到文件扫描 (error=%s)', _db_error)
    _t_fallback_start = _time.time()
    files = _list_conversation_files()
    if date:
        files = [f for f in files if os.path.basename(os.path.dirname(f)) == date]
    read_count = max(limit * 5, limit)
    files = files[:read_count]
    items: list[dict[str, Any]] = []
    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        conversation_id = doc.get('conversation_id') or os.path.splitext(os.path.basename(fp))[0]
        route_name = doc.get('route', '')
        last_model = doc.get('last_client_model', '')
        updated_at = doc.get('updated_at', '')
        created_at = doc.get('created_at', '')
        turn_count = int(doc.get('turn_count', 0) or 0)
        if q:
            q_lower = q.lower()
            hay = ' '.join([str(conversation_id), str(route_name), str(last_model)]).lower()
            if q_lower not in hay:
                continue
        items.append({
            'conversation_id': conversation_id,
            'date': os.path.basename(os.path.dirname(fp)),
            'route': route_name,
            'last_client_model': last_model,
            'last_backend': doc.get('last_backend', ''),
            'created_at': created_at,
            'updated_at': updated_at,
            'turn_count': turn_count,
            'note': (notes.get(conversation_id) or {}).get('note', ''),
        })
        if len(items) >= limit:
            break
    t = (_time.time() - _start) * 1000
    t_fallback = (_time.time() - _t_fallback_start) * 1000
    logger.warning('[性能] GET /api/admin/logs 文件回退 扫描%d个文件 返回%d条 fallback=%.0fms total=%.0fms',
                   len(files), len(items), t_fallback, t)
    return jsonify({'items': items})


@bp.route('/api/admin/logs/<path:conversation_id>', methods=['GET'])
def logs_detail(conversation_id: str):
    """查看某个会话日志的完整内容（优先数据库）。

    支持参数:
      ?turn=N            只返回指定 turn（索引从 0 开始），默认返回第一个
      ?turn_id=xxx       按 turn_id 精确查找
      ?fields=summary    只返回元数据 + turn 摘要，不返回消息体和流式事件
    """
    import time as _time
    _start = _time.time()
    err = _check_auth()
    if err:
        return err

    turn_idx = request.args.get('turn', '').strip()
    turn_idx = int(turn_idx) if turn_idx.lstrip('-').isdigit() else None
    turn_id = (request.args.get('turn_id') or '').strip() or None

    # 优先从数据库读取
    try:
        detail = conversation_store.get_turn_detail(
            conversation_id,
            turn_index=turn_idx if turn_idx is not None else (None if turn_id else 0),
            turn_id=turn_id,
        )
    except Exception:
        detail = None

    if detail:
        notes = _load_log_notes()
        note_entry = notes.get(conversation_id) or {}
        result = {
            'conversation': detail,
            'note': note_entry.get('note', ''),
        }
        total_ms = (_time.time() - _start) * 1000
        logger.info('[性能] GET /api/admin/logs/%s?turn=%s DB 总turns=%d 耗时%.0fms',
                    conversation_id, turn_idx if turn_idx is not None else 0,
                    detail.get('_total_turns', 0), total_ms)
        return jsonify(result)

    # 回退：DB 无数据，走文件路径
    date = (request.args.get('date') or '').strip() or None
    if turn_idx is not None and date:
        turn_fp = os.path.join(_LOG_DIR, date, f'{conversation_id}_turn{turn_idx}.json')
        if os.path.isfile(turn_fp):
            try:
                with open(turn_fp, 'r', encoding='utf-8') as f:
                    raw = f.read()
                doc = json.loads(raw)
                notes = _load_log_notes()
                note_entry = notes.get(conversation_id) or {}
                result = {'conversation': doc, 'note': note_entry.get('note', '')}
                total_ms = (_time.time() - _start) * 1000
                logger.info('[性能] GET /api/admin/logs/%s?turn=%d turn文件回退 大小=%.0fKB 耗时%.0fms',
                            conversation_id, turn_idx, len(raw) / 1024, total_ms)
                return jsonify(result)
            except (OSError, json.JSONDecodeError):
                pass

    fp = _find_conversation_file(conversation_id, date)
    if not fp:
        return jsonify({'error': '日志不存在'}), 404

    try:
        with open(fp, 'r', encoding='utf-8') as f:
            raw = f.read()
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return jsonify({'error': '日志读取失败'}), 500

    if turn_idx is not None:
        all_turns = doc.get('turns', [])
        if 0 <= turn_idx < len(all_turns):
            doc['turns'] = [all_turns[turn_idx]]
            doc['_current_turn'] = turn_idx
            doc['_total_turns'] = len(all_turns)
        else:
            return jsonify({'error': 'turn 索引超出范围', 'total_turns': len(all_turns)}), 400

    notes = _load_log_notes()
    note_entry = notes.get(conversation_id) or {}
    result = {'conversation': doc, 'note': note_entry.get('note', '')}
    total_ms = (_time.time() - _start) * 1000
    logger.info('[性能] GET /api/admin/logs/%s 文件回退 大小=%.0fKB 耗时%.0fms',
                conversation_id, len(raw) / 1024, total_ms)
    return jsonify(result)


@bp.route('/api/admin/logs/<path:conversation_id>', methods=['DELETE'])
def logs_delete(conversation_id: str):
    """删除某个会话日志（数据库 + 文件）。"""
    err = _check_auth()
    if err:
        return err

    try:
        conversation_store.delete_conversation(conversation_id)
    except Exception:
        pass

    # 删除所有相关 JSON 文件
    for fp in _find_conversation_files_all(conversation_id):
        try:
            os.remove(fp)
        except OSError:
            pass

    conv_idx.delete_conversation(conversation_id)

    notes = _load_log_notes()
    if conversation_id in notes:
        notes.pop(conversation_id, None)
        _save_log_notes(notes)

    return jsonify({'ok': True})


@bp.route('/api/admin/logs/batch-delete', methods=['POST'])
def logs_batch_delete():
    """批量删除会话日志（DB + 文件）。"""
    err = _check_auth()
    if err:
        return err
    data = request.get_json(force=True, silent=True) or {}
    ids = data.get('ids') or []
    if not ids or not isinstance(ids, list):
        return jsonify({'error': '请提供 ids 列表'}), 400
    deleted = 0
    for conv_id in ids:
        try:
            conversation_store.delete_conversation(str(conv_id))
            # 同步删除 JSON 文件
            for fp in _find_conversation_files_all(str(conv_id)):
                try:
                    os.remove(fp)
                except OSError:
                    pass
            deleted += 1
        except Exception as e:
            logger.warning('删除会话 %s 失败: %s', conv_id, e)
    return jsonify({'ok': True, 'deleted': deleted, 'total': len(ids)})


def _find_conversation_files_all(conv_id: str) -> list[str]:
    """查找某个会话的所有相关文件（完整文件 + turn 分片）。"""
    if not os.path.isdir(_LOG_DIR):
        return []
    results = []
    for day_dir in glob.glob(os.path.join(_LOG_DIR, '*')):
        if not os.path.isdir(day_dir):
            continue
        for pattern in [f'{conv_id}.json', f'{conv_id}_turn*.json']:
            for fp in glob.glob(os.path.join(day_dir, pattern)):
                results.append(fp)
    return results


# ─── 修复规则管理 ─────────────────────────────────

_FIXES_FILE = os.path.join(DATA_DIR, 'error_fixes.json')
_FIXES_LOCK = threading.Lock()


def _load_fixes() -> dict[str, Any]:
    with _FIXES_LOCK:
        if not os.path.exists(_FIXES_FILE):
            return {'fixes': []}
        try:
            with open(_FIXES_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {'fixes': []}
        except (OSError, json.JSONDecodeError):
            return {'fixes': []}


def _save_fixes(data: dict[str, Any]) -> None:
    with _FIXES_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(_FIXES_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


@bp.route('/api/admin/error-fixes', methods=['GET'])
def list_error_fixes():
    err = _check_auth()
    if err: return err
    return jsonify(_load_fixes())


@bp.route('/api/admin/error-fixes', methods=['POST'])
def add_error_fix():
    err = _check_auth()
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    fixes = _load_fixes()
    import time as _t
    entry = {
        'id': 'fix_' + str(int(_t.time())),
        'error_pattern': str(data.get('error_pattern', '')),
        'upstream_llm': str(data.get('upstream_llm', '')),
        'problem': str(data.get('problem', '')),
        'fix_description': str(data.get('fix_description', '')),
        'fix_version': str(data.get('fix_version', '')),
        'patch_handler': str(data.get('patch_handler', '')),
        'status': 'active',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    fixes.setdefault('fixes', []).append(entry)
    _save_fixes(fixes)
    return jsonify({'ok': True, 'fix': entry})


@bp.route('/api/admin/error-fixes/<fix_id>', methods=['PUT'])
def update_error_fix(fix_id):
    err = _check_auth()
    if err: return err
    data = request.get_json(force=True, silent=True) or {}
    fixes = _load_fixes()
    for f in fixes.get('fixes', []):
        if f.get('id') == fix_id:
            if 'status' in data: f['status'] = data['status']
            if 'fix_description' in data: f['fix_description'] = data['fix_description']
            if 'problem' in data: f['problem'] = data['problem']
            _save_fixes(fixes)
            return jsonify({'ok': True, 'fix': f})
    return jsonify({'error': '未找到'}), 404


@bp.route('/api/admin/error-fixes/<fix_id>', methods=['DELETE'])
def delete_error_fix(fix_id):
    err = _check_auth()
    if err: return err
    fixes = _load_fixes()
    fixes['fixes'] = [f for f in fixes.get('fixes', []) if f.get('id') != fix_id]
    _save_fixes(fixes)
    return jsonify({'ok': True})


@bp.route('/admin/status')
@bp.route('/admin/status/')
def admin_status_page():
    """返回服务器状态仪表盘页面。"""
    return send_from_directory(_STATIC_DIR, 'status.html')


@bp.route('/api/admin/logs/live-stream', methods=['GET'])
def logs_live_stream():
    """SSE 实时日志流（给状态仪表盘用）。"""
    err = _check_auth_with_query_key()
    if err: return err
    from utils import log_stream

    def gen():
        q = log_stream.subscribe()
        try:
            yield sse_data_message({'type': 'hello'})
            while True:
                try:
                    entry = q.get(timeout=2)
                except queue.Empty:
                    yield sse_data_message({'type': 'ping'})
                    continue
                yield sse_data_message({'type': 'log', 'entry': entry})
        finally:
            log_stream.unsubscribe(q)

    return sse_response(gen())


@bp.route('/api/admin/logs/recent', methods=['GET'])
def logs_recent():
    """获取最近 N 条日志（用于初始加载）。"""
    err = _check_auth()
    if err: return err
    from utils import log_stream
    n = int(request.args.get('n', '50'))
    return jsonify({'entries': log_stream.get_recent(n)})


def _get_cpu_pct() -> float:
    """瞬时 CPU 使用率：读两次 /proc/stat，间隔 0.5s，取差值。

    返回整个系统的 CPU 使用率百分比 (0~100)。
    """
    import time as _time
    try:
        with open('/proc/stat') as f:
            f1 = [int(x) for x in f.readline().split()[1:]]
        _time.sleep(0.5)
        with open('/proc/stat') as f:
            f2 = [int(x) for x in f.readline().split()[1:]]
        idle_delta = f2[3] - f1[3]
        total_delta = sum(f2) - sum(f1)
        return round((1 - idle_delta / total_delta) * 100, 1) if total_delta > 0 else 0.0
    except Exception:
        return 0.0


def _get_host_mem() -> tuple[int, int]:
    """宿主机内存 (匹配宝塔面板)：读 /proc/meminfo。

    Docker 默认不虚拟化 /proc/meminfo，所以容器内读到的就是宿主机数据。
    返回 (used_mb, total_mb)。
    """
    try:
        mem_total = mem_avail = 0
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                if line.startswith('MemAvailable:'):
                    mem_avail = int(line.split()[1])
        if mem_total:
            total_mb = mem_total // 1024
            used_mb = (mem_total - mem_avail) // 1024
            return (used_mb, total_mb)
    except Exception:
        pass
    return (0, 0)


@bp.route('/api/admin/server-status', methods=['GET'])
def server_status():
    """返回服务器实时状态数据。"""
    err = _check_auth()
    if err: return err
    import os as _os, threading

    # 内存（宿主机级别，匹配宝塔面板）
    mem_used_mb, mem_limit_mb = _get_host_mem()

    # CPU（瞬时值，采样两次）
    cpu_pct = _get_cpu_pct()

    # 磁盘
    disk_free = 0
    disk_used_pct = 0
    try:
        st = _os.statvfs(DATA_DIR)
        disk_free = round(st.f_frsize * st.f_bavail / 1024 / 1024 / 1024, 1)
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        disk_used_pct = round((1 - free / total) * 100, 1) if total > 0 else 0
    except Exception:
        pass

    # 打开文件数（当前进程）
    open_files = 0
    try:
        open_files = len(_os.listdir(f'/proc/{_os.getpid()}/fd'))
    except Exception:
        pass

    # 运行时间
    uptime = 0
    try:
        with open('/proc/uptime') as f:
            uptime = int(float(f.readline().split()[0]))
    except Exception:
        pass

    stats = {
        'server': {
            'uptime_seconds': uptime,
            'time': datetime.now(timezone.utc).isoformat(),
        },
        'resources': {
            'cpu_percent': cpu_pct,
            'memory_used_mb': mem_used_mb,
            'memory_total_mb': mem_limit_mb,
            'disk_free_gb': disk_free,
            'disk_used_percent': disk_used_pct,
            'threads': threading.active_count(),
            'open_files': open_files,
        },
        'proxy': {
            'total_requests': _STATS['total_requests'],
            'total_errors': _STATS['total_errors'],
            'active_streams': _STATS['active_streams'],
            'requests_last_min': _STATS['requests_last_min'],
            'patch_applied': _STATS['patch_applied'],
            'patch_success': _STATS['patch_success'],
            'patch_failed': _STATS['patch_failed'],
        },
        'upstream': {
            'url': settings.get_url(),
            'health': _check_upstream_health(),
        },
        'recent_errors': list(_STATS['recent_errors'])[-10:],
    }

    # 会话统计
    try:
        stats['conversations'] = {
            'total': conversation_store.get_conversation_count(),
            'with_errors': len(conversation_store.list_conversations(limit=1000, fix_status='has_error')),
        }
    except Exception:
        stats['conversations'] = {'total': 0, 'with_errors': 0}

    return jsonify(stats)


# ─── 运行时统计 ────────────────────────────────
_STATS = {
    'total_requests': 0,
    'total_errors': 0,
    'active_streams': 0,
    'requests_last_min': 0,
    'patch_applied': 0,
    'patch_success': 0,
    'patch_failed': 0,
    'recent_errors': [],
}
_STATS_LOCK = threading.Lock()
_last_min_counter = [0, 0.0]  # [count, window_start]

def record_request():
    global _last_min_counter
    with _STATS_LOCK:
        _STATS['total_requests'] += 1
        now = time.time()
        if now - _last_min_counter[1] > 60:
            _STATS['requests_last_min'] = _last_min_counter[0]
            _last_min_counter = [1, now]
        else:
            _last_min_counter[0] += 1

def record_error(msg: str):
    global _STATS
    with _STATS_LOCK:
        _STATS['total_errors'] += 1
        _STATS['recent_errors'].append({
            'time': datetime.now(timezone.utc).isoformat(),
            'message': msg[:200],
        })
        if len(_STATS['recent_errors']) > 50:
            _STATS['recent_errors'] = _STATS['recent_errors'][-50:]

def record_patch(success: bool):
    global _STATS
    with _STATS_LOCK:
        _STATS['patch_applied'] += 1
        if success:
            _STATS['patch_success'] += 1
        else:
            _STATS['patch_failed'] += 1

def stream_started():
    global _STATS
    with _STATS_LOCK:
        _STATS['active_streams'] += 1

def stream_ended():
    global _STATS
    with _STATS_LOCK:
        _STATS['active_streams'] = max(0, _STATS['active_streams'] - 1)

def _check_upstream_health():
    import requests as _r
    try:
        t0 = time.time()
        r = _r.get(settings.get_url().rstrip('/') + '/models', timeout=5,
                   headers={'Authorization': 'Bearer ' + (settings.get_key() or 'none')})
        ms = int((time.time() - t0) * 1000)
        return {'ok': r.status_code < 500, 'status': r.status_code, 'latency_ms': ms}
    except Exception as e:
        return {'ok': False, 'status': 0, 'latency_ms': 0, 'error': str(e)[:100]}


@bp.route('/api/admin/logs/search', methods=['GET'])
def logs_search():
    """全文搜索消息内容。"""
    err = _check_auth()
    if err:
        return err
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({'error': '搜索词至少 2 个字符'}), 400
    try:
        results = conversation_store.search_messages(q, limit=50)
        return jsonify({'items': results, 'q': q})
    except Exception as e:
        logger.warning('搜索失败: %s', e)
        return jsonify({'error': '搜索暂时不可用'}), 500


@bp.route('/api/admin/logs/clear', methods=['POST'])
def logs_clear():
    """清空历史会话日志目录，以 NDJSON 流式返回进度（每行一个 JSON 对象）。"""
    err = _check_auth()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    if not data.get('confirm'):
        return jsonify({'error': '需要 confirm=true 才能清空'}), 400

    def ndjson_progress():
        try:
            if not os.path.isdir(_LOG_DIR):
                yield json.dumps(
                    {'phase': 'start', 'total': 0}, ensure_ascii=False
                ) + '\n'
                yield json.dumps(
                    {'phase': 'done', 'removed': 0, 'errors': 0, 'total': 0},
                    ensure_ascii=False,
                ) + '\n'
                conv_idx.clear_all_rows()
            try:
                conversation_store.clear_all()
            except Exception:
                pass
                return

            files = glob.glob(os.path.join(_LOG_DIR, '*', '*.json'))
            total = len(files)
            yield json.dumps({'phase': 'start', 'total': total}, ensure_ascii=False) + '\n'

            errors = 0
            removed = 0
            # 进度推送次数约 ≤100，避免海量文件时刷屏
            step = max(1, total // 100) if total > 0 else 1

            for i, fp in enumerate(files):
                try:
                    os.remove(fp)
                    removed += 1
                except OSError:
                    errors += 1

                done = i + 1
                if done == total or done % step == 0 or total <= 20:
                    yield json.dumps(
                        {
                            'phase': 'progress',
                            'done': done,
                            'total': total,
                            'errors': errors,
                            'current': os.path.basename(fp),
                        },
                        ensure_ascii=False,
                    ) + '\n'

            yield json.dumps(
                {
                    'phase': 'done',
                    'removed': removed,
                    'errors': errors,
                    'total': total,
                },
                ensure_ascii=False,
            ) + '\n'
            conv_idx.clear_all_rows()
            try:
                conversation_store.clear_all()
            except Exception:
                pass
        except Exception as e:
            logger.exception('清空历史日志异常')
            yield json.dumps({'phase': 'error', 'message': str(e)}, ensure_ascii=False) + '\n'

    return Response(
        stream_with_context(ndjson_progress()),
        mimetype='application/x-ndjson; charset=utf-8',
        headers={
            'Cache-Control': 'no-store',
            'X-Accel-Buffering': 'no',
        },
    )


@bp.route('/api/admin/logs/<path:conversation_id>/note', methods=['PUT'])
def logs_update_note(conversation_id: str):
    """为某个会话日志添加/更新备注。"""
    err = _check_auth()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    note = str(data.get('note') or '')
    if len(note) > 2000:
        return jsonify({'error': 'note too long (max 2000)'}), 400

    notes = _load_log_notes()
    entry = notes.get(conversation_id) or {}
    entry['note'] = note
    notes[conversation_id] = entry
    _save_log_notes(notes)

    return jsonify({'ok': True})
