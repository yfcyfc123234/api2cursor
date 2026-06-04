"""回填 v1→v2 缺失字段：client_type, client_user, client_requests"""
import json, glob, os, sqlite3

db_path = '/app/data/conversations.db'
conn = sqlite3.connect(db_path)
conn.execute('PRAGMA journal_mode=WAL')

files = glob.glob('/app/data/conversations/*/*.json')
files = [f for f in files if '_turn' not in os.path.basename(f)]

updated = 0
for fp in files:
    try:
        with open(fp) as f:
            doc = json.load(f)
    except Exception:
        continue
    for turn in doc.get('turns', []):
        tid = turn.get('turn_id', '')
        if not tid:
            continue
        cr = turn.get('client_request') or {}
        rh = turn.get('request_headers') or {}
        ua = rh.get('User-Agent', rh.get('user-agent', ''))
        ct = 'cursor' if 'cursor' in ua.lower() else ('other' if ua else 'unknown')
        conn.execute(
            "UPDATE turns SET client_user=?, client_stream_options=?, client_tools_json=?, client_type=? WHERE id=?",
            (cr.get('user', '') or '',
             json.dumps(cr.get('stream_options')) if cr.get('stream_options') else None,
             json.dumps(cr.get('tools')) if cr.get('tools') else None, ct, tid))
        conn.execute(
            "INSERT OR IGNORE INTO client_requests (turn_id, model, user_field, stream_options, tools_json, headers) VALUES (?,?,?,?,?,?)",
            (tid, cr.get('model', ''), cr.get('user', '') or '',
             json.dumps(cr.get('stream_options')) if cr.get('stream_options') else None,
             json.dumps(cr.get('tools')) if cr.get('tools') else None,
             json.dumps(rh) if rh else None))
        updated += 1

conn.commit()
conn.close()
print(f'回填完成: {updated} 条 turn')
