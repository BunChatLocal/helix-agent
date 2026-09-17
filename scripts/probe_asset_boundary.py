"""Check which documentation images a pupil can fetch, against the real workspace.

The assets folder is the whole knowledge base, including screenshots of the Azure
PIM and MFA-reset procedures. A pupil should get the images their own two guides
cite, and 404 on everything else.
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18791"


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def fetch(name, token):
    req = urllib.request.Request(
        BASE + "/api/assets/" + name, headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as exc:
        return exc.code


def main():
    from core.config import get_config
    from core.context_engine import PUPIL_KNOWLEDGE

    ws = Path(get_config().workspace_path)
    assets = sorted(p.name for p in (ws / "assets").iterdir() if p.is_file())

    cited = set()
    for rel in PUPIL_KNOWLEDGE:
        try:
            cited.update(re.findall(
                r"asset:([A-Za-z0-9._-]+)",
                (ws / rel).read_text(encoding="utf-8", errors="replace"),
            ))
        except OSError:
            pass

    pupil = post("/api/auth/dev-login", {"email": "elev@afk.no", "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]
    staff = post("/api/auth/dev-login", {"email": "ansatt@test.no", "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]

    print(f"{len(assets)} images in the knowledge base, "
          f"{len(cited)} cited by the pupil guides\n")

    wrong = 0
    for name in assets:
        p, s = fetch(name, pupil), fetch(name, staff)
        expected = 200 if name in cited else 404
        ok = p == expected and s == 200
        if not ok:
            wrong += 1
        print(f"  {'ok ' if ok else 'BAD'} pupil {p}  staff {s}  "
              f"{'cited' if name in cited else 'not cited':<10} {name}")

    print(f"\n{'OK — the boundary holds.' if not wrong else f'{wrong} wrong.'}")


main()
