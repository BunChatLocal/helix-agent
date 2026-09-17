"""Check the retention window each tier is told about, and the keep behaviour.

Manual verification aid for the per-tier retention decision in ROLES.md.
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18791"


def post(path: str, body: dict, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=240) as r:
        return json.loads(r.read())


def get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


for email in ("elev@afk.no", "ansatt@test.no"):
    token = post("/api/auth/dev-login", {"email": email, "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]
    me = get("/api/me", token)
    convs = get("/api/me/conversations", token)["conversations"]
    print(f"{email:<18} kept {me['retention_days']} days")
    for c in convs:
        print(
            f"   {c['expires_in_days']} days left, {c['message_count']} messages, "
            f"keepable={c['can_keep']} — {c['title']}"
        )

    # Keep the newest conversation and see which path it takes.
    if convs:
        cid = convs[0]["id"]
        try:
            out = post(f"/api/me/conversations/{cid}/compact", {}, token)
            print(f"   keep → {out.get('kept')}")
            if out.get("summary"):
                print(f"   note: {out['summary'].strip()[:300]}")
        except urllib.error.HTTPError as exc:
            print(f"   keep → refused {exc.code}: {exc.read().decode()[:120]}")
