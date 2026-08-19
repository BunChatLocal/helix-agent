"""Print the newest pupil-tier conversation, for probing the scope restriction by hand."""
import json
import pathlib
import sqlite3

root = pathlib.Path.home() / ".helix" / "sessions" / "helix"
db = sqlite3.connect(root / "sessions.db")
row = db.execute(
    "SELECT session_id, peer, tier FROM sessions "
    "WHERE peer LIKE 'dev:elev@afk.no|%' ORDER BY last_active DESC LIMIT 1"
).fetchone()

if not row:
    raise SystemExit("no pupil session found")

session_id, peer, tier = row
print(f"session={session_id} peer={peer} tier={tier}\n")

with open(root / f"{session_id}.jsonl", encoding="utf-8") as fh:
    for line in fh:
        msg = json.loads(line)
        print(msg["role"].upper(), ":", msg["content"][:1500], "\n")
