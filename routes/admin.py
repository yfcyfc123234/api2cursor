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
        'stream_trace 事件为完整列表；否则可能仅保留头尾若干条（中间折叠计数），详见 request_logger。\n'
    )


@bp.route('/api/admin/logs/export', methods=['POST'])
def logs_export_zip():
    """导出会话日志等为 ZIP（默认全部，或按时间范围）。"""
    err = _check_auth()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    export_all = bool(data.get('all', False))
    start_s = (data.get('start') or '').strip()
    end_s = (data.get('end') or '').strip()

    start_dt = _parse_iso_dt(start_s) if start_s else None
    end_dt = _parse_iso_dt(end_s) if end_s else None

    if not export_all:
        if start_dt is None or end_dt is None:
            return jsonify({'error': {'message': '请设置 all=true，或同时提供 start 与 end（ISO8601）', 'type': 'bad_request'}}), 400
        if start_dt > end_dt:
            return jsonify({'error': {'message': 'start 不能晚于 end', 'type': 'bad_request'}}), 400

    files = _list_conversation_files()
    included: list[str] = []
    for fp in files:
        if export_all:
            included.append(fp)
            continue
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(doc, dict) and _conversation_doc_in_time_range(doc, start_dt, end_dt):
            included.append(fp)

    buf = io.BytesIO()
    exported_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        manifest: dict[str, Any] = {
            'type': 'api2cursor_logs_export',
            'version': 1,
            'exported_at': exported_at,
            'export_all': export_all,
            'time_range': None if export_all else {'start': start_s, 'end': end_s},
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
    fname = f'api2cursor-logs-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")}.zip'
    return send_file(
        buf,
        mimetype='application/zip',
        as_attachment=True,
        download_name=fname,
        max_age=0,
    )


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


@bp.route('/api/admin/logs/count', methods=['GET'])
def logs_count():
    """返回与「清空历史」相同规则下的会话 JSON 文件数量，供前端判断是否可清空。"""
    err = _check_auth()
    if err:
        return err

    n = 0
    if os.path.isdir(_LOG_DIR):
        n = len(glob.glob(os.path.join(_LOG_DIR, '*', '*.json')))
    return jsonify({'count': n})


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
