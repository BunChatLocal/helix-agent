"""Hold a long pupil conversation, then keep it, and read the note they get.

The solution-only summary is a prompt, so a mock cannot tell us whether it
actually produces something a pupil can follow. This does.
"""

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18791"
TURNS = [
    "hei, jeg får ikke koblet den nye pc-en min til wifi på skolen",
    "ja jeg er på skolen nå. jeg ser bare FRID-iot i listen",
    "ok jeg er koblet til FRID-iot, hva gjør jeg nå?",
    "jeg har lastet ned sertifikatet, men jeg vet ikke hvor det ble lagret",
    "nå ligger det i Nedlastinger. hva gjør jeg med det?",
    "det funket! nå er jeg på nettet. tusen takk",
]


def post(path, body, token=""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(), headers=headers
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def get(path, token):
    req = urllib.request.Request(
        BASE + path, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def main():
    import websockets

    token = post("/api/auth/dev-login", {"email": "elev-lang@afk.no", "password": "helix-probe-login", "password_confirm": "helix-probe-login"})["token"]
    cid = post("/api/me/conversations", {}, token)["id"]

    async with websockets.connect("ws://127.0.0.1:18791/ws/chat") as ws:
        await ws.send(json.dumps({"token": token, "conversation_id": cid}))
        while True:
            if json.loads(await ws.recv()).get("status") == "connected":
                break
        for i, text in enumerate(TURNS, 1):
            await ws.send(json.dumps({"message": text}))
            parts = []
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=240))
                if frame.get("chunk"):
                    parts.append(frame["chunk"])
                if frame.get("done"):
                    break
            print(f"[{i}/{len(TURNS)}] {''.join(parts).strip()[:160]}")

    conv = next(
        c for c in get("/api/me/conversations", token)["conversations"] if c["id"] == cid
    )
    print(f"\n{conv['message_count']} messages, {conv['expires_in_days']} days left")

    out = post(f"/api/me/conversations/{cid}/compact", {}, token)
    print(f"\nkeep → {out.get('kept')}, in_thread={out.get('in_thread')}")
    print("\n--- the note the pupil keeps ---")
    print(out.get("summary"))

    after = next(
        c for c in get("/api/me/conversations", token)["conversations"] if c["id"] == cid
    )
    print(f"\nclock now: {after['expires_in_days']} days left")


asyncio.run(main())
