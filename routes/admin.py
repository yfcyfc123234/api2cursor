"""路由: 管理面板

提供 Web 管理界面和 API：
  - /admin         — 管理面板页面
  - /v1/models     — 模型列表（供 Cursor 查询）
  - /api/admin/*   — 登录验证、全局设置 CRUD、模型映射 CRUD
"""

import os
import logging
import json
import glob
import queue
import threading
from typing import Any

from flask import Blueprint, request, jsonify, send_from_directory

import settings
from config import Config
from settings import DATA_DIR
from utils.http import sse_response
from routes.common import sse_data_message
from utils import request_logger as request_logger_mod

logger = logging.getLogger(__name__)

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'static')

bp = Blueprint('admin', __name__)


# ─── 静态页面 ─────────────────────────────────────


@bp.route('/admin')
@bp.route('/admin/')
def admin_page():
    """返回管理面板首页 HTML 页面，供浏览器进入配置界面。"""
    return send_from_directory(_STATIC_DIR, 'admin.html')


@bp.route('/admin/logs')
@bp.route('/admin/logs/')
def admin_logs_page():
    """返回日志调试页。"""
    return send_from_directory(_STATIC_DIR, 'admin_logs.html')


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
    for key in ('proxy_target_url', 'proxy_api_key', 'debug_mode'):
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
    return jsonify(usage_tracker.get_stats())


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


def _check_auth_with_query_key() -> Any | None:
    """Admin API 鉴权：优先支持 query key（用于 EventSource 无自定义 header 场景）。"""
    if not Config.ACCESS_API_KEY:
        return None
    token = request.args.get('key', '') or request.headers.get('Authorization', '')
    if isinstance(token, str) and token.startswith('Bearer '):
        token = token[7:]
    if not token:
        token = request.headers.get('x-api-key', '')
    if token != Config.ACCESS_API_KEY:
        return jsonify({'error': '未授权'}), 401
    return None


def _find_conversation_file(conversation_id: str, date: str | None = None) -> str | None:
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


@bp.route('/api/admin/logs/live', methods=['GET'])
def logs_live_sse():
    """实时推送 verbose 模式下的 request/response 日志事件。"""
    err = _check_auth_with_query_key()
    if err:
        return err

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
                yield sse_data_message(evt)
        finally:
            request_logger_mod.unregister_live_subscriber(q)

    return sse_response(gen())


@bp.route('/api/admin/logs', methods=['GET'])
def logs_list():
    """列出最近的会话日志（历史）。"""
    err = _check_auth()
    if err:
        return err

    limit = int(request.args.get('limit', '30'))
    q = (request.args.get('q') or '').strip()
    date = (request.args.get('date') or '').strip() or None

    notes = _load_log_notes()

    files = _list_conversation_files()
    if date:
        files = [f for f in files if os.path.basename(os.path.dirname(f)) == date]

    # 如果要做 q 过滤，先多读一点，避免过滤后数量不足
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

    return jsonify({'items': items})


@bp.route('/api/admin/logs/<path:conversation_id>', methods=['GET'])
def logs_detail(conversation_id: str):
    """查看某个会话日志的完整内容。"""
    err = _check_auth()
    if err:
        return err

    date = (request.args.get('date') or '').strip() or None
    fp = _find_conversation_file(conversation_id, date)
    if not fp:
        return jsonify({'error': '日志不存在'}), 404

    try:
        with open(fp, 'r', encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return jsonify({'error': '日志读取失败'}), 500

    notes = _load_log_notes()
    note_entry = notes.get(conversation_id) or {}
    return jsonify({
        'conversation': doc,
        'note': note_entry.get('note', ''),
    })


@bp.route('/api/admin/logs/<path:conversation_id>', methods=['DELETE'])
def logs_delete(conversation_id: str):
    """删除某个会话日志文件。"""
    err = _check_auth()
    if err:
        return err

    date = (request.args.get('date') or '').strip() or None
    fp = _find_conversation_file(conversation_id, date)
    if not fp:
        return jsonify({'ok': True})

    try:
        os.remove(fp)
    except OSError as e:
        return jsonify({'error': {'message': f'delete failed: {e}', 'type': 'delete_error'}}), 500

    notes = _load_log_notes()
    if conversation_id in notes:
        notes.pop(conversation_id, None)
        _save_log_notes(notes)

    return jsonify({'ok': True})


@bp.route('/api/admin/logs/clear', methods=['POST'])
def logs_clear():
    """清空历史会话日志目录。"""
    err = _check_auth()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    if not data.get('confirm'):
        return jsonify({'error': '需要 confirm=true 才能清空'}), 400

    if os.path.isdir(_LOG_DIR):
        files = glob.glob(os.path.join(_LOG_DIR, '*', '*.json'))
        for fp in files:
            try:
                os.remove(fp)
            except OSError:
                pass

    return jsonify({'ok': True})


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
