"""Drive a few pupil conversations, then read the insights the admin view shows.

Manual verification aid: proves the audit records are written on the real code
path (websocket, live agent) rather than only under test doubles.
"""

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18791"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get(path: str, token: str) -> dict:
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


async def ask(token: str, messages: list[str]) -> list[str]:
    import websockets

    replies = []
    async with websockets.connect(f"ws://127.0.0.1:18791/ws/chat") as ws:
        await ws.send(json.dumps({"token": token}))
        while True:
            frame = json.loads(await ws.recv())
            if frame.get("status") == "connected":
                break
        for text in messages:
            await ws.send(json.dumps({"message": text}))
            parts = []
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=180))
                if frame.get("chunk"):
                    parts.append(frame["chunk"])
                if frame.get("done"):
                    break
            replies.append("".join(parts))
    return replies


async def main():
    if "--read-only" not in sys.argv:
        pupil = post("/api/auth/dev-login", {"email": "elev@afk.no", "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]
        print("— pupil asks about wifi, then asks for something off-topic —")
        for reply in await ask(pupil, [
            "hei, jeg får ikke koblet pc-en min til wifi på skolen",
            "kan du skrive et dikt om host til norsktimen?",
        ]):
            print("\n>>", reply.strip()[:400])

        other = post("/api/auth/dev-login", {"email": "elev2@afk.no", "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]
        await ask(other, ["jeg har glemt passordet mitt, hva gjør jeg?"])

    # Same JWT secret as the running server, so this is a real operator token.
    from web.app import _create_token
    admin = _create_token(sub="admin", role="admin")
    data = get("/api/insights?days=7", admin)

    print("\n— what the admin view would show —")
    print("totals:", data["totals"])
    print("by tier:", data["by_tier"])
    print("by domain:", data["by_domain"])
    print("top words:", [(w["key"], w["count"]) for w in data["top_words"][:8]])
    for c in data["recent"]:
        print(f"  {c['tier']:<8} {c['email']:<20} {c['title']}")
    for r in data["recent_refusals"]:
        print(f"  REFUSED {r['email']:<20} {r['asked']}")


asyncio.run(main())
