#!/usr/bin/env python3
"""会话数据清理脚本

策略：
  1. 默认只保留最近 24 小时的会话
  2. 磁盘使用率超过阈值时，缩短保留时间到 6 小时
  3. 磁盘使用率超过紧急阈值时，只保留最近 2 小时
  4. 清理后执行 VACUUM 回收磁盘空间

用法:
  python scripts/cleanup_old_data.py              # 手动执行一次
  python scripts/cleanup_old_data.py --dry-run    # 预览，不实际删除

可配合 crontab 定期执行:
  */30 * * * * cd /app && python scripts/cleanup_old_data.py >> /app/data/cleanup.log 2>&1
"""

import argparse
import logging
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta

# ─── 配置 ────────────────────────────────────────────

DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data'))
DB_PATH = os.path.join(DATA_DIR, 'conversations.db')
INDEX_PATH = os.path.join(DATA_DIR, 'conversation_index.sqlite3')
JSON_DIR = os.path.join(DATA_DIR, 'conversations')

DEFAULT_RETENTION_HOURS = 24       # 默认保留 24 小时
HIGH_DISK_RETENTION_HOURS = 6      # 磁盘紧张时保留 6 小时
CRITICAL_RETENTION_HOURS = 2       # 磁盘紧急时保留 2 小时

DISK_WARN_THRESHOLD = 0.75         # 磁盘使用率 ≥75% → 缩短保留时间
DISK_CRITICAL_THRESHOLD = 0.88     # 磁盘使用率 ≥88% → 紧急清理

# 需要清理的数据库表（按外键依赖顺序）
TABLES_BY_DEPENDENCY = [
    'stream_events',
    'messages',
    'upstream_requests',
    'client_requests',
    'turns',
    'conversations',
]

logger = logging.getLogger('cleanup')


# ─── 磁盘检查 ────────────────────────────────────────

def get_disk_usage(path: str = DATA_DIR) -> float:
    """返回磁盘使用率 (0.0 ~ 1.0)。"""
    try:
        stat = os.statvfs(path)
        total = stat.f_frsize * stat.f_blocks
        free = stat.f_frsize * stat.f_bavail
        return 1.0 - (free / total) if total > 0 else 0.0
    except OSError:
        return 0.0


def get_disk_free_gb(path: str = DATA_DIR) -> float:
    """返回磁盘剩余空间 (GB)。"""
    try:
        stat = os.statvfs(path)
        return round(stat.f_frsize * stat.f_bavail / 1024 / 1024 / 1024, 1)
    except OSError:
        return 0.0


# ─── 数据库清理 ──────────────────────────────────────

def get_db_size_mb(db_path: str) -> float:
    """返回数据库文件大小 (MB)。"""
    try:
        return os.path.getsize(db_path) / 1024 / 1024
    except OSError:
        return 0.0


def get_conversation_count(db_path: str) -> int:
    """返回当前会话总数。"""
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.execute('PRAGMA journal_mode=WAL')
        n = conn.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
        conn.close()
        return n
    except Exception:
        return -1


def delete_old_conversations(db_path: str, cutoff_iso: str, dry_run: bool = False) -> int:
    """删除 updated_at 早于 cutoff_iso 的所有会话（级联删除关联数据）。

    返回删除的会话数。
    """
    if not os.path.exists(db_path):
        logger.info('数据库文件不存在，跳过')
        return 0

    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')

    try:
        # 找出要删除的 conversation IDs
        old_ids = [
            r[0] for r in conn.execute(
                "SELECT id FROM conversations WHERE updated_at < ?", (cutoff_iso,)
            ).fetchall()
        ]

        if not old_ids:
            return 0

        logger.info('找到 %d 个过期会话 (updated_at < %s)', len(old_ids), cutoff_iso)

        if dry_run:
            logger.info('[DRY-RUN] 将删除 %d 个会话 (跳过实际删除)', len(old_ids))
            return len(old_ids)

        # 按依赖顺序逐表删除
        deleted = 0
        for conv_id in old_ids:
            # 删除该会话的所有关联数据
            for turn_row in conn.execute(
                "SELECT id FROM turns WHERE conversation_id = ?", (conv_id,)
            ).fetchall():
                tid = turn_row[0]
                conn.execute("DELETE FROM stream_events WHERE turn_id = ?", (tid,))
                conn.execute("DELETE FROM messages WHERE turn_id = ?", (tid,))
                conn.execute("DELETE FROM upstream_requests WHERE turn_id = ?", (tid,))
                conn.execute("DELETE FROM client_requests WHERE turn_id = ?", (tid,))
            conn.execute("DELETE FROM turns WHERE conversation_id = ?", (conv_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
            deleted += 1

        conn.commit()
        logger.info('已删除 %d 个会话', deleted)
        return deleted

    finally:
        conn.close()


def vacuum_db(db_path: str) -> None:
    """VACUUM 回收数据库空间。"""
    if not os.path.exists(db_path):
        return
    size_before = get_db_size_mb(db_path)
    conn = sqlite3.connect(db_path, timeout=300)
    try:
        conn.execute('VACUUM')
    finally:
        conn.close()
    size_after = get_db_size_mb(db_path)
    if size_before > size_after:
        logger.info('VACUUM: %.1f MB → %.1f MB (回收 %.1f MB)',
                    size_before, size_after, size_before - size_after)


# ─── JSON 文件清理 ────────────────────────────────────

def delete_old_json_files(json_dir: str, cutoff_iso: str, dry_run: bool = False) -> int:
    """删除 updated_at 早于 cutoff_iso 的 JSON 文件。

    也清理对应的日期子目录（如果为空）。
    返回删除的文件数。
    """
    if not os.path.isdir(json_dir):
        return 0

    import json
    deleted = 0

    for day_name in sorted(os.listdir(json_dir)):
        day_dir = os.path.join(json_dir, day_name)
        if not os.path.isdir(day_dir):
            continue

        for fname in os.listdir(day_dir):
            if not fname.endswith('.json'):
                continue
            fpath = os.path.join(day_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    doc = json.load(f)
                updated = str(doc.get('updated_at', '') or '')
                if updated and updated < cutoff_iso:
                    if not dry_run:
                        os.remove(fpath)
                    deleted += 1
            except (OSError, json.JSONDecodeError, ValueError):
                # 损坏的文件直接清理
                if not dry_run:
                    try:
                        os.remove(fpath)
                    except OSError:
                        pass
                deleted += 1

        # 删除空目录
        if not dry_run:
            try:
                remaining = [f for f in os.listdir(day_dir) if f.endswith('.json')]
                if not remaining:
                    os.rmdir(day_dir)
            except OSError:
                pass

    if deleted:
        logger.info('已删除 %d 个过期 JSON 文件', deleted)
    return deleted


# ─── 索引清理 ─────────────────────────────────────────

def cleanup_index(index_path: str, cutoff_iso: str, dry_run: bool = False) -> int:
    """清理 conversation_index 中的过期行。"""
    if not os.path.exists(index_path):
        return 0

    conn = sqlite3.connect(index_path, timeout=30)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM conversation_index WHERE updated_at < ?",
            (cutoff_iso,),
        ).fetchone()[0]
        if n and not dry_run:
            conn.execute(
                "DELETE FROM conversation_index WHERE updated_at < ?",
                (cutoff_iso,),
            )
            conn.commit()
        return n or 0
    finally:
        conn.close()


# ─── 主逻辑 ───────────────────────────────────────────

def determine_retention_hours(disk_usage: float) -> int:
    """根据磁盘使用率决定保留时长。"""
    if disk_usage >= DISK_CRITICAL_THRESHOLD:
        return CRITICAL_RETENTION_HOURS
    if disk_usage >= DISK_WARN_THRESHOLD:
        return HIGH_DISK_RETENTION_HOURS
    return DEFAULT_RETENTION_HOURS


def run_cleanup(dry_run: bool = False) -> dict:
    """执行一次完整清理，返回统计信息。"""
    stats = {
        'disk_usage_before': get_disk_usage(DATA_DIR),
        'disk_free_gb_before': get_disk_free_gb(DATA_DIR),
        'db_size_mb_before': get_db_size_mb(DB_PATH),
        'conv_count_before': get_conversation_count(DB_PATH),
        'retention_hours': 0,
        'deleted_convs': 0,
        'deleted_json': 0,
        'deleted_index': 0,
        'vacuumed': False,
        'disk_usage_after': 0.0,
        'disk_free_gb_after': 0.0,
        'db_size_mb_after': 0.0,
        'conv_count_after': 0,
    }

    disk_usage = stats['disk_usage_before']
    retention_hours = determine_retention_hours(disk_usage)
    stats['retention_hours'] = retention_hours

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=retention_hours)).isoformat()

    logger.info(
        '磁盘使用率 %.0f%% (剩余 %.1f GB) → 保留最近 %d 小时 (cutoff=%s)',
        disk_usage * 100, stats['disk_free_gb_before'], retention_hours, cutoff,
    )

    if dry_run:
        logger.info('*** DRY-RUN 模式，不会实际删除 ***')

    # 1. 清理数据库
    logger.info('清理 conversations.db …')
    stats['deleted_convs'] = delete_old_conversations(DB_PATH, cutoff, dry_run)

    # 2. 清理 JSON 文件
    logger.info('清理 JSON 文件 …')
    stats['deleted_json'] = delete_old_json_files(JSON_DIR, cutoff, dry_run)

    # 3. 清理索引
    logger.info('清理 conversation_index …')
    stats['deleted_index'] = cleanup_index(INDEX_PATH, cutoff, dry_run)

    # 4. 如果磁盘仍然紧张或删除了大量数据，执行 VACUUM
    total_deleted = stats['deleted_convs'] + stats['deleted_json']
    if total_deleted > 50 or disk_usage >= DISK_WARN_THRESHOLD:
        logger.info('执行 VACUUM …')
        if not dry_run:
            vacuum_db(DB_PATH)
            stats['vacuumed'] = True

    # 5. 收尾统计
    stats['disk_usage_after'] = get_disk_usage(DATA_DIR)
    stats['disk_free_gb_after'] = get_disk_free_gb(DATA_DIR)
    stats['db_size_mb_after'] = get_db_size_mb(DB_PATH)
    stats['conv_count_after'] = get_conversation_count(DB_PATH)

    return stats


def log_summary(stats: dict) -> None:
    """输出清理摘要。"""
    logger.info('=' * 55)
    logger.info('清理完成')
    logger.info('  磁盘使用率: %.0f%% → %.0f%%',
                stats['disk_usage_before'] * 100, stats['disk_usage_after'] * 100)
    logger.info('  磁盘剩余: %.1f GB → %.1f GB',
                stats['disk_free_gb_before'], stats['disk_free_gb_after'])
    logger.info('  数据库大小: %.1f MB → %.1f MB',
                stats['db_size_mb_before'], stats['db_size_mb_after'])
    logger.info('  会话数: %d → %d',
                stats['conv_count_before'], stats.get('conv_count_after', 0))
    logger.info('  删除: %d 个会话 + %d 个JSON文件 + %d 条索引',
                stats['deleted_convs'], stats['deleted_json'], stats['deleted_index'])
    if stats['vacuumed']:
        logger.info('  VACUUM: 已执行')
    logger.info('=' * 55)


# ─── CLI ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='api2cursor 会话数据清理')
    parser.add_argument('--dry-run', action='store_true', help='预览模式，不实际删除')
    parser.add_argument('--quiet', '-q', action='store_true', help='静默模式（仅输出错误）')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format='%(asctime)s [cleanup] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    os.makedirs(DATA_DIR, exist_ok=True)

    stats = run_cleanup(dry_run=args.dry_run)
    log_summary(stats)

    if stats['disk_usage_after'] >= DISK_CRITICAL_THRESHOLD and not args.dry_run:
        logger.error('⚠ 清理后磁盘仍超过 %.0f%%，请手动排查！', DISK_CRITICAL_THRESHOLD * 100)
        sys.exit(1)

    sys.exit(0)


if __name__ == '__main__':
    main()
