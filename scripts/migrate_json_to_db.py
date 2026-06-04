"""一次性脚本：将 data/conversations/ 下所有 JSON 迁移到 SQLite

用法:
  cd /www/wwwroot/api2cursor
  python scripts/migrate_json_to_db.py

安全：不删除原 JSON 文件，仅写入 DB。可重复运行（跳过已存在的记录）。
"""

import glob
import json
import os
import sys
import time as _time

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import conversation_store

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'conversations')


def main():
    files = glob.glob(os.path.join(_LOG_DIR, '*', '*.json'))
    # 排除 turn 分片文件（它们只是完整文件的子集）
    files = [f for f in files if '_turn' not in os.path.basename(f)]

    if not files:
        print('没有找到 JSON 会话文件')
        return

    print(f'找到 {len(files)} 个会话文件，开始迁移...')
    ok_count = 0
    skip_count = 0
    err_count = 0
    t0 = _time.time()

    for fp in files:
        try:
            with open(fp, 'r', encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f'  SKIP 读取失败: {fp} ({e})')
            err_count += 1
            continue

        if not isinstance(doc, dict):
            skip_count += 1
            continue

        conv_id = doc.get('conversation_id') or os.path.splitext(os.path.basename(fp))[0]
        turns = doc.get('turns') or []

        for turn in turns:
            if not isinstance(turn, dict):
                continue
            # 确保 turn 有 conversation_id
            if not turn.get('conversation_id'):
                turn['conversation_id'] = conv_id
            if not turn.get('turn_id'):
                turn['turn_id'] = turn.get('id', '') or f'{conv_id}_t{turns.index(turn)}'
            try:
                conversation_store.insert_turn(turn)
            except Exception as e:
                print(f'  写入失败 turn={turn.get("turn_id")}: {e}')
                err_count += 1
                continue

        ok_count += 1
        if ok_count % 5 == 0:
            print(f'  进度: {ok_count}/{len(files)} 会话...')

    elapsed = _time.time() - t0
    total_turns = conversation_store.get_conversation_count()
    print(f'\n迁移完成: {ok_count} 个会话, {err_count} 个错误, {skip_count} 个跳过')
    print(f'数据库中共 {total_turns} 个会话')
    print(f'耗时: {elapsed:.1f}s')


if __name__ == '__main__':
    main()
