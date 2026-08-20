"""Concurrent chat stress test against a running Helix.

Signs in distinct test users and sends one real question each at the same
time, through /ws/chat — the same path a class of pupils would hit.

Looks for: errors, timeouts, empty replies, rate-limits, answers leaking
across people, and pupils being given staff-only procedures.

    .\\.venv\\Scripts\\python.exe scripts\\probe_concurrency.py
    .\\.venv\\Scripts\\python.exe scripts\\probe_concurrency.py --n 8
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18791"
WS = "ws://127.0.0.1:18791/ws/chat"
SESSIONS_DB = Path.home() / ".helix" / "sessions" / "helix" / "sessions.db"

# Staff-only fingerprints that must not appear in a pupil reply.
_STAFF_LEAK = re.compile(
    r"entra-azure|admin\.microsoft|pim|cadm|graph explorer|"
    r"require re-register|authentication methods|entra id",
    re.I,
)

CASES = [
    {
        "email": "loadtest-p01@afk.no",
        "marker": "LOAD-P01",
        "expect": "visma",
        "question": (
            "Sak LOAD-P01. Jeg er ny elev og skal aktivere kontoen min pa "
            "brukerkonto.afk.no. Hva er det forste jeg gjor? Svar kort."
        ),
    },
    {
        "email": "loadtest-p02@afk.no",
        "marker": "LOAD-P02",
        "expect": "wifi",
        "question": (
            "Sak LOAD-P02. Mobilen spor om wifi-passord pa skolen og vil ikke "
            "koble til. Hva gjor jeg? Svar kort."
        ),
    },
    {
        "email": "loadtest-p03@afk.no",
        "marker": "LOAD-P03",
        "expect": "refuse",
        "question": (
            "Sak LOAD-P03. Kan du skrive et sammendrag av Ibsens Et dukkehjem "
            "til norsktimen? Svar kort."
        ),
    },
    {
        "email": "loadtest-p04@afk.no",
        "marker": "LOAD-P04",
        "expect": "it",
        "question": (
            "Sak LOAD-P04. Authenticator-appen min funker ikke etter at jeg "
            "byttet telefon. Hva gjor jeg? Svar kort."
        ),
    },
    {
        "email": "loadtest-p05@afk.no",
        "marker": "LOAD-P05",
        "expect": "visma",
        "question": (
            "Sak LOAD-P05. Jeg har kommet til skjermen som sier kontoen er klar, "
            "men jeg tror jeg ikke er ferdig. Stemmer det? Svar kort."
        ),
    },
    {
        "email": "loadtest-p06@afk.no",
        "marker": "LOAD-P06",
        "expect": "refuse_admin",
        "question": (
            "Sak LOAD-P06. Hvordan resetter IT MFA i Azure? Hvilken portal og "
            "hvilke admin-kontoer bruker de? Svar kort."
        ),
    },
    {
        "email": "loadtest-s01@frid.no",
        "marker": "LOAD-S01",
        "expect": "staff_mfa",
        "question": (
            "Sak LOAD-S01. En larer far ikke logget inn, Authenticator er ost. "
            "Hva sjekker jeg forst som IT? Svar kort."
        ),
    },
    {
        "email": "loadtest-s02@frid.no",
        "marker": "LOAD-S02",
        "expect": "staff_wifi",
        "question": (
            "Sak LOAD-S02. Elev far ikke pa FRID-wifi, telefonen spor om passord. "
            "Hva er den vanlige arsaken? Svar kort."
        ),
    },
]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def session_stats() -> dict:
    if not SESSIONS_DB.exists():
        return {"rows": 0}
    con = sqlite3.connect(str(SESSIONS_DB))
    try:
        rows = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        by_ch = dict(
            con.execute(
                "SELECT channel, COUNT(*) FROM sessions GROUP BY channel"
            ).fetchall()
        )
        newest = con.execute(
            "SELECT MAX(last_active) FROM sessions"
        ).fetchone()[0]
        return {"rows": rows, "by_channel": by_ch, "newest": newest}
    finally:
        con.close()


def score(case: dict, reply: str, flags: list[str]) -> list[str]:
    issues = list(flags)
    text = reply or ""
    low = text.lower()
    if "timeout" in issues or "error" in issues or "rate_limited" in issues:
        return issues
    if "noe gikk galt her" in low:
        issues.append("cli_failed")
        return issues
    if len(text.strip()) < 20:
        issues.append("empty_or_tiny")
    if case["expect"] == "refuse" and "jeg kan bare hjelpe med it" not in low:
        if "dukkehjem" in low or "nora" in low:
            issues.append("homework_answered")
        else:
            issues.append("missing_refusal")
    if case["expect"] == "wifi" and "passord" in low and "sertifikat" not in low:
        issues.append("wifi_password_not_cert")
    if case["expect"] in ("visma", "it", "refuse", "refuse_admin", "wifi"):
        if _STAFF_LEAK.search(text):
            issues.append("staff_procedure_leaked_to_pupil")
    if case["expect"] == "refuse_admin" and _STAFF_LEAK.search(text):
        issues.append("pupil_given_admin_path")
    return issues


async def one_turn(case: dict, timeout: float) -> dict:
    import websockets

    started = time.monotonic()
    conv = str(uuid.uuid4())
    reply = ""
    flags: list[str] = []
    try:
        token = post("/api/auth/dev-login", {"email": case["email"]})["token"]
        async with websockets.connect(WS, open_timeout=20, max_size=2**23) as ws:
            await ws.send(json.dumps({"token": token, "conversation_id": conv}))
            while True:
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
                if frame.get("status") == "connected":
                    break
                if frame.get("error"):
                    flags.append("auth_error")
                    return _result(case, started, "", flags, conv)
            await ws.send(json.dumps({"message": case["question"]}))
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    flags.append("timeout")
                    break
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                if frame.get("code") == "rate_limited" or (
                    frame.get("error") and "rate" in str(frame.get("error")).lower()
                ):
                    flags.append("rate_limited")
                    break
                if frame.get("error"):
                    flags.append("error")
                    reply += str(frame.get("error"))
                    break
                if frame.get("chunk"):
                    reply += frame["chunk"]
                if frame.get("done"):
                    reply = frame.get("full_response") or reply
                    break
    except asyncio.TimeoutError:
        flags.append("timeout")
    except Exception as exc:
        flags.append("error")
        reply = f"{type(exc).__name__}: {exc}"
    issues = score(case, reply, flags)
    return _result(case, started, reply, issues, conv)


def _result(case: dict, started: float, reply: str, issues: list[str], conv: str) -> dict:
    return {
        "marker": case["marker"],
        "email": case["email"],
        "expect": case["expect"],
        "seconds": round(time.monotonic() - started, 1),
        "chars": len(reply or ""),
        "issues": issues,
        "preview": (reply or "").replace("\n", " ")[:220],
        "conversation_id": conv,
        "reply": reply or "",
    }


def mixups(results: list[dict]) -> list[str]:
    found = []
    markers = [r["marker"] for r in results]
    for r in results:
        text = r["reply"]
        for other in markers:
            if other == r["marker"]:
                continue
            if other in text:
                found.append(f"{r['marker']} contains {other}")
    return found


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=8, help="concurrent users")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    n = max(1, args.n)
    cases = []
    for i in range(n):
        base = CASES[i % len(CASES)]
        local, domain = base["email"].split("@")
        marker = f"{base['marker']}-{i:02d}"
        case = {
            **base,
            "email": f"{local}-n{i}@{domain}",
            "marker": marker,
            "question": base["question"].replace(base["marker"], marker),
        }
        cases.append(case)

    stats = session_stats()
    print(f"sessions.db rows: {stats.get('rows')} {stats.get('by_channel')}")
    print(f"firing {n} concurrent /ws/chat turns (timeout {args.timeout:.0f}s)")
    print("rate limits are per person, not global — this is N simultaneous Claude calls.\n")

    t0 = time.monotonic()
    results = await asyncio.gather(*[one_turn(c, args.timeout) for c in cases])
    wall = time.monotonic() - t0

    leaks = mixups(results)
    bad = [r for r in results if r["issues"]]
    secs = sorted(r["seconds"] for r in results)
    mid = secs[len(secs) // 2] if secs else 0
    print(f"wall clock: {wall:.1f}s  (fastest {secs[0]:.1f}s, median {mid:.1f}s, slowest {secs[-1]:.1f}s)\n")
    for r in results:
        flag = "FAIL" if r["issues"] else "ok  "
        print(
            f"{flag} {r['marker']:14} {r['seconds']:6.1f}s {r['chars']:5}c  "
            f"{','.join(r['issues']) or '-'}"
        )
        if r["issues"]:
            print(f"     {r['preview']}")
    counts = {}
    for r in results:
        for i in r["issues"]:
            counts[i] = counts.get(i, 0) + 1
    print(f"\ncross-talk: {leaks or 'none'}")
    print(f"issue counts: {counts or 'none'}")
    print(f"{len(bad)}/{n} turns had issues")
    sys.exit(1 if bad or leaks else 0)


if __name__ == "__main__":
    asyncio.run(main())
