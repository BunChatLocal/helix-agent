"""
zorken.chat/aihub — owner-only reverse proxy to a personal AI Hub at home.

Not part of Zorken itself. It is OFF unless AIHUB_UPSTREAM is set, and only the
emails in AIHUB_EMAILS (who must also still be admins in Zorken) get through.
To take it out completely: delete this file and the short block at the end of
web/app.py that installs it (marked "AI Hub").

Railway variables
  AIHUB_UPSTREAM        hub on the home PC over Tailscale, e.g. http://j-rgen:8090
  AIHUB_COMFY_UPSTREAM  ComfyUI on the home PC,           e.g. http://j-rgen:8188
  AIHUB_KEY             shared secret; the hub only answers requests carrying it
  AIHUB_EMAILS          comma-separated, e.g. jorgenthin@frid-iks.no
  AIHUB_HTTP_PROXY      tailscaled's userspace proxy (set by deploy/entrypoint.sh),
                        e.g. http://127.0.0.1:1055
  TS_AUTHKEY            Tailscale auth key for this container (entrypoint.sh)

Flow
  1. The owner opens /aihub/. Not signed in → a small page takes the Zorken token
     from localStorage (helix_token) and POSTs /api/aihub/session with it.
  2. That endpoint checks the token, the email and live admin access, then sets an
     HttpOnly cookie (path /aihub, SameSite=Strict, 12 h). The cookie carries
     aud="aihub", so it can never be used as a Zorken API token.
  3. Every /aihub request re-checks the cookie, the email list and live access,
     then forwards to the home PC with X-AIHub-Key. Zorken's own cookies and
     Authorization header are never forwarded. /aihub/comfy/... goes to ComfyUI
     (HTTP and its /ws websocket).
"""

import asyncio
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import aiohttp
import jwt
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse

logger = logging.getLogger("helix.aihub")

COOKIE = "zk_aihub"
PREFIX = "/aihub"
COMFY_PREFIX = "/aihub/comfy"
SESSION_HOURS = 12
# Hop-by-hop and identity headers that must not cross the proxy.
_DROP_REQ = {"host", "cookie", "authorization", "origin", "referer", "connection", "keep-alive",
             "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
             "content-length", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "x-real-ip",
             "x-aihub-key", "x-aihub-user", "x-aihub-comfy-base"}
_DROP_RESP = {"connection", "keep-alive", "transfer-encoding", "content-length", "set-cookie",
              "proxy-authenticate", "trailer", "upgrade"}


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def enabled() -> bool:
    return bool(_env("AIHUB_UPSTREAM") and _env("AIHUB_KEY") and allowed_emails())


def allowed_emails() -> set[str]:
    return {e.strip().lower() for e in _env("AIHUB_EMAILS").split(",") if e.strip()}


GATE_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Hub</title>
<style>body{font:15px/1.5 system-ui,sans-serif;background:#1f1e1d;color:#ecebe8;display:flex;
align-items:center;justify-content:center;min-height:100vh;margin:0}div{max-width:420px;padding:24px;text-align:center}
a{color:#e8a587}</style></head><body><div id="m">Signing in…</div><script>
(async () => {
  const m = document.getElementById('m');
  const t = localStorage.getItem('helix_token');
  if (!t) { m.innerHTML = 'Sign in to <a href="/admin">Zorken admin</a> first, then come back to <a href="/aihub/">/aihub</a>.'; return; }
  try {
    const r = await fetch('/api/aihub/session', { method: 'POST', headers: { Authorization: 'Bearer ' + t } });
    if (r.ok) { location.replace('/aihub/'); return; }
    m.innerHTML = r.status === 401 ? 'Your Zorken sign-in has expired. <a href="/admin">Sign in again</a>, then come back.'
                                   : 'This page is not available for your account.';
  } catch (e) { m.textContent = 'Could not reach Zorken: ' + e; }
})();
</script></body></html>"""


class _Hub:
    def __init__(self, decode_token: Callable[[str], Optional[dict]], console_kind: Callable[[dict], Optional[str]],
                 claims_email: Callable[[dict], str], jwt_secret: Callable[[], str]):
        self.decode_token = decode_token
        self.console_kind = console_kind
        self.claims_email = claims_email
        self.jwt_secret = jwt_secret
        self._session: Optional[aiohttp.ClientSession] = None

    # ── who may pass ─────────────────────────────────────────────────────────
    def _owner_ok(self, email: str, tier: str = "") -> bool:
        """Email on the list AND still an admin in Zorken right now (live overrides)."""
        email = (email or "").strip().lower()
        if not email or email not in allowed_emails():
            return False
        return self.console_kind({"role": "user", "email": email, "tier": tier}) in ("admin", "operator")

    def _mint(self, email: str, tier: str) -> str:
        exp = datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)
        return jwt.encode({"aud": "aihub", "email": email, "tier": tier, "exp": exp}, self.jwt_secret(), algorithm="HS256")

    def _cookie_email(self, raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        try:
            c = jwt.decode(raw, self.jwt_secret(), algorithms=["HS256"], audience="aihub")
        except Exception:
            return None
        email = (c.get("email") or "").lower()
        return email if self._owner_ok(email, c.get("tier") or "") else None

    # ── upstream ─────────────────────────────────────────────────────────────
    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=3600))
        return self._session

    @staticmethod
    def _target(path: str) -> tuple[str, str, bool]:
        """(upstream base, path on upstream, is_comfy)"""
        if path == "comfy" or path.startswith("comfy/"):
            base = _env("AIHUB_COMFY_UPSTREAM") or _env("AIHUB_UPSTREAM").rsplit(":", 1)[0] + ":8188"
            return base.rstrip("/"), "/" + path[len("comfy"):].lstrip("/"), True
        return _env("AIHUB_UPSTREAM").rstrip("/"), "/" + path, False

    def _headers(self, request_headers, email: str, comfy: bool) -> dict:
        h = {k: v for k, v in request_headers.items() if k.lower() not in _DROP_REQ}
        if not comfy:   # the hub checks this key; ComfyUI is only reachable inside the tailnet anyway
            h["X-AIHub-Key"] = _env("AIHUB_KEY")
            h["X-AIHub-User"] = email
            h["X-AIHub-Comfy-Base"] = COMFY_PREFIX
        return h

    async def proxy(self, request: Request, path: str) -> Response:
        email = self._cookie_email(request.cookies.get(COOKIE))
        if not email:
            if request.method == "GET" and "text/html" in (request.headers.get("accept") or ""):
                return HTMLResponse(GATE_HTML, headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "Sign in at /aihub first."}, status_code=401)
        base, upath, comfy = self._target(path)
        url = base + upath + (("?" + request.url.query) if request.url.query else "")
        body = await request.body()
        try:
            up = await self._http().request(
                request.method, url, data=body if body else None,
                headers=self._headers(request.headers, email, comfy),
                proxy=_env("AIHUB_HTTP_PROXY") or None, allow_redirects=False)
        except Exception as e:
            logger.warning("aihub upstream error for %s: %r", upath, e)
            return HTMLResponse(
                "<h3>The AI Hub at home is not reachable.</h3><p>Is the PC on, AI Hub running and Tailscale connected?</p>",
                status_code=502)
        headers = {k: v for k, v in up.headers.items() if k.lower() not in _DROP_RESP}
        loc = headers.get("Location") or headers.get("location")
        if loc and loc.startswith("/"):
            headers.pop("location", None)
            headers["Location"] = (COMFY_PREFIX if comfy else PREFIX) + loc
        headers["X-Robots-Tag"] = "noindex"

        async def stream():
            try:
                async for chunk in up.content.iter_any():
                    yield chunk
            finally:
                up.release()
        return StreamingResponse(stream(), status_code=up.status, headers=headers)

    async def websocket(self, ws: WebSocket, path: str) -> None:
        email = self._cookie_email(ws.cookies.get(COOKIE))
        if not email:
            await ws.close(code=4401)
            return
        base, upath, comfy = self._target(path)
        if not comfy:
            await ws.close(code=4404)
            return
        url = base.replace("http://", "ws://").replace("https://", "wss://") + upath
        if ws.url.query:
            url += "?" + ws.url.query
        await ws.accept()
        try:
            async with self._http().ws_connect(url, proxy=_env("AIHUB_HTTP_PROXY") or None,
                                               heartbeat=30, max_msg_size=0) as up:
                async def down():
                    async for msg in up:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws.send_bytes(msg.data)
                        else:
                            break

                async def upstream():
                    while True:
                        m = await ws.receive()
                        if m.get("type") == "websocket.disconnect":
                            break
                        if m.get("text") is not None:
                            await up.send_str(m["text"])
                        elif m.get("bytes") is not None:
                            await up.send_bytes(m["bytes"])

                tasks = [asyncio.create_task(down()), asyncio.create_task(upstream())]
                _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        except Exception as e:
            logger.warning("aihub websocket error: %r", e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass


def install(app: FastAPI, *, decode_token, console_kind, claims_email, jwt_secret) -> None:
    hub = _Hub(decode_token, console_kind, claims_email, jwt_secret)

    @app.post("/api/aihub/session")
    async def aihub_session(request: Request):
        auth = request.headers.get("authorization") or ""
        claims = decode_token(auth[7:]) if auth.lower().startswith("bearer ") else None
        if not claims:
            raise HTTPException(401, "Unauthorized")
        email = claims_email(claims)
        tier = claims.get("tier") or ""
        if not hub._owner_ok(email, tier):
            logger.warning("aihub: refused session for %r", email or claims.get("sub"))
            raise HTTPException(403, "Not available for this account")
        local = (request.url.hostname or "") in ("127.0.0.1", "localhost")
        resp = JSONResponse({"ok": True, "url": PREFIX + "/"})
        resp.set_cookie(COOKIE, hub._mint(email, tier), max_age=SESSION_HOURS * 3600, path=PREFIX,
                        httponly=True, secure=not local, samesite="strict")
        logger.info("aihub: session for %s", email)
        return resp

    @app.post("/api/aihub/logout")
    async def aihub_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path=PREFIX)
        return resp

    @app.get(PREFIX)
    async def aihub_root():
        return RedirectResponse(PREFIX + "/", status_code=302)

    @app.api_route(PREFIX + "/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def aihub_proxy(request: Request, path: str):
        return await hub.proxy(request, path)

    @app.websocket(COMFY_PREFIX + "/{path:path}")
    async def aihub_ws(ws: WebSocket, path: str):
        await hub.websocket(ws, "comfy/" + path)

    logger.info("aihub: enabled for %s -> %s", ", ".join(sorted(allowed_emails())), _env("AIHUB_UPSTREAM"))
