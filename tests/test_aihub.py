"""zorken.chat/aihub: owner-only proxy (web/aihub.py).

Runs a fake "home PC" (hub + ComfyUI) with aiohttp and drives a minimal FastAPI
app that installs the proxy the same way web/app.py does.
"""
import asyncio
import socket
import threading
import time

import jwt
import pytest
from aiohttp import web

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from web import aihub  # noqa: E402

SECRET = "test-secret-0123456789abcdef0123456789"
KEY = "shared-key-123"
OWNER = "owner@frid-iks.no"
SEEN: dict = {}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _fake_home(port_hub: int, port_comfy: int):
    async def hub_any(request):
        SEEN["hub"] = {"path": request.path_qs, "headers": dict(request.headers), "body": await request.read()}
        if request.headers.get("X-AIHub-Key") != KEY:
            return web.json_response({"error": "forbidden"}, status=403)
        if request.path == "/api/stream":
            resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await resp.prepare(request)
            for i in range(3):
                await resp.write(f"data: {i}\n\n".encode())
                await asyncio.sleep(0.05)
            await resp.write_eof()
            return resp
        return web.json_response({"ok": True, "path": request.path})

    async def comfy_any(request):
        if request.path == "/ws":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            SEEN["comfy_ws_query"] = request.query_string
            await ws.send_str("hello")
            async for msg in ws:
                await ws.send_str("echo:" + msg.data)
            return ws
        SEEN["comfy"] = {"path": request.path_qs, "headers": dict(request.headers)}
        return web.Response(text="comfy " + request.path)

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for port, handler in ((port_hub, hub_any), (port_comfy, comfy_any)):
            a = web.Application()
            a.router.add_route("*", "/{tail:.*}", handler)
            r = web.AppRunner(a)
            loop.run_until_complete(r.setup())
            loop.run_until_complete(web.TCPSite(r, "127.0.0.1", port).start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.5)


@pytest.fixture(scope="module")
def client():
    ph, pc = _free_port(), _free_port()
    _fake_home(ph, pc)
    mp = pytest.MonkeyPatch()
    mp.setenv("AIHUB_UPSTREAM", f"http://127.0.0.1:{ph}")
    mp.setenv("AIHUB_COMFY_UPSTREAM", f"http://127.0.0.1:{pc}")
    mp.setenv("AIHUB_KEY", KEY)
    mp.setenv("AIHUB_EMAILS", OWNER)
    mp.delenv("AIHUB_HTTP_PROXY", raising=False)
    assert aihub.enabled()

    def decode(tok):
        try:
            return jwt.decode(tok, SECRET, algorithms=["HS256"])
        except Exception:
            return None

    def console_kind(claims):
        return "admin" if claims.get("tier") == "admin" else None

    app = FastAPI()
    aihub.install(app, decode_token=decode, console_kind=console_kind,
                  claims_email=lambda c: (c.get("email") or "").lower(), jwt_secret=lambda: SECRET)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    mp.undo()


def _token(email, tier="admin"):
    return jwt.encode({"sub": "dev:" + email, "role": "user", "email": email, "tier": tier,
                       "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")


def _login(client):
    r = client.post("/api/aihub/session", headers={"Authorization": "Bearer " + _token(OWNER)})
    assert r.status_code == 200, r.text
    return r


def test_without_cookie_gate_page_or_401(client):
    client.cookies.clear()
    r = client.get("/aihub/", headers={"Accept": "text/html"})
    assert r.status_code == 200 and "helix_token" in r.text
    assert client.get("/aihub/api/stats", headers={"Accept": "application/json"}).status_code == 401


def test_session_refuses_others(client):
    client.cookies.clear()
    assert client.post("/api/aihub/session").status_code == 401
    assert client.post("/api/aihub/session", headers={"Authorization": "Bearer junk"}).status_code == 401
    other = client.post("/api/aihub/session", headers={"Authorization": "Bearer " + _token("coworker@frid-iks.no")})
    assert other.status_code == 403
    demoted = client.post("/api/aihub/session", headers={"Authorization": "Bearer " + _token(OWNER, tier="staff")})
    assert demoted.status_code == 403


def test_cookie_is_not_a_zorken_token(client):
    r = _login(client)
    cookie = r.cookies.get(aihub.COOKIE)
    with pytest.raises(jwt.InvalidAudienceError):
        jwt.decode(cookie, SECRET, algorithms=["HS256"])     # how web/app.py decodes Bearer tokens


def test_proxy_adds_key_and_strips_identity(client):
    _login(client)
    r = client.get("/aihub/api/stats?x=1", headers={"Authorization": "Bearer zorken-token", "Origin": "https://zorken.chat"})
    assert r.status_code == 200 and r.json()["path"] == "/api/stats"
    h = {k.lower(): v for k, v in SEEN["hub"]["headers"].items()}
    assert SEEN["hub"]["path"] == "/api/stats?x=1"
    assert h["x-aihub-key"] == KEY and h["x-aihub-user"] == OWNER and h["x-aihub-comfy-base"] == "/aihub/comfy"
    assert "authorization" not in h and "cookie" not in h and "origin" not in h


def test_post_body_and_stream(client):
    _login(client)
    r = client.post("/aihub/api/chats", json={"kind": "chat"})
    assert r.status_code == 200 and b'"kind": "chat"' in SEEN["hub"]["body"].replace(b'":"', b'": "')
    with client.stream("POST", "/aihub/api/stream", json={}) as s:
        body = b"".join(s.iter_raw())
    assert body == b"data: 0\n\ndata: 1\n\ndata: 2\n\n"


def test_comfy_http_and_websocket(client):
    cookie = _login(client).cookies.get(aihub.COOKIE)
    r = client.get("/aihub/comfy/api/object_info", headers={"Origin": "https://zorken.chat"})
    assert r.text == "comfy /api/object_info"
    h = {k.lower() for k in SEEN["comfy"]["headers"]}
    assert "x-aihub-key" not in h and "origin" not in h and "cookie" not in h
    # the test client does not attach its cookie jar to websockets; browsers do (same site, path /aihub)
    with client.websocket_connect("/aihub/comfy/ws?clientId=abc", headers={"Cookie": f"{aihub.COOKIE}={cookie}"}) as ws:
        assert ws.receive_text() == "hello"
        ws.send_text("ping")
        assert ws.receive_text() == "echo:ping"
    assert SEEN["comfy_ws_query"] == "clientId=abc"


def test_removing_email_locks_out_existing_cookie(client, monkeypatch):
    _login(client)
    monkeypatch.setenv("AIHUB_EMAILS", "someone-else@frid-iks.no")
    assert client.get("/aihub/api/stats", headers={"Accept": "application/json"}).status_code == 401


def test_websocket_without_cookie_refused(client):
    client.cookies.clear()
    with pytest.raises(Exception):
        with client.websocket_connect("/aihub/comfy/ws") as ws:
            ws.receive_text()
