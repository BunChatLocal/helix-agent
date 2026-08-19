"""Check that the chat rate limit belongs to the person, not to the socket.

Uses /status, a slash command: it is rate-checked like any other message but
never reaches Claude, so this costs nothing to run.
"""

import asyncio
import json
import sys
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


async def burst(token, n, label):
    """Send n messages on a fresh connection. Returns how many were refused."""
    import websockets

    refused = 0
    async with websockets.connect("ws://127.0.0.1:18791/ws/chat") as ws:
        await ws.send(json.dumps({"token": token}))
        while True:
            if json.loads(await ws.recv()).get("status") == "connected":
                break
        for i in range(n):
            await ws.send(json.dumps({"message": "/status"}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if frame.get("error"):
                    refused += 1
                    if refused == 1:
                        print(f"   {label} refused at message {i + 1}: "
                              f"{frame['error']} (code={frame.get('code')})")
                    break
                if frame.get("done"):
                    break
    return refused


async def main():
    from core.config import get_config
    limits = get_config().security.rate_limit
    staff_limit = limits.for_tier("staff")[0]
    print(f"limits — staff {limits.for_tier('staff')}, "
          f"pupil {limits.for_tier('student')} (per minute, per hour)")

    token = post("/api/auth/dev-login", {"email": "ratetest@frid.no"})["token"]

    half = staff_limit // 2
    first = await burst(token, half, "connection 1")
    print(f"connection 1: {half} sent, {first} refused")

    # New connection, same person. The old limiter reset here; this must not.
    second = await burst(token, staff_limit, "connection 2")
    print(f"connection 2: {staff_limit} sent, {second} refused")

    if second:
        print("\nOK — the allowance carried across the reconnect.")
    else:
        print("\nFAIL — connection 2 got a fresh allowance.")

    # A different person must be unaffected by the first one's spending.
    other = post("/api/auth/dev-login", {"email": "ratetest2@frid.no"})["token"]
    third = await burst(other, 3, "other person")
    print(f"other person: 3 sent, {third} refused "
          f"({'OK' if third == 0 else 'FAIL — limits are not per person'})")


asyncio.run(main())
