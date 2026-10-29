# zorken.chat/aihub (owner-only, optional)

A private route that shows the owner's AI Hub (a chat/image app running on a PC
at home) inside zorken.chat. **It is not part of Zorken.** It is off unless the
variables below are set, and only listed emails that are *still admins in
Zorken* get through.

Remove it entirely: delete `web/aihub.py`, `tests/test_aihub.py`, this file, the
"AI Hub" block at the end of `web/app.py`, the Tailscale `RUN` step in the
`Dockerfile` and the Tailscale block in `deploy/entrypoint.sh`.

## How it works

```
browser ── https://zorken.chat/aihub/… ── Zorken (Railway) ── Tailscale ── home PC
                                              │                            ├─ AI Hub  :8090
                                              │ checks login + email       └─ ComfyUI :8188
```

1. Open `https://zorken.chat/aihub/`. If there is no AI Hub cookie yet, a small
   page takes your Zorken sign-in (the same token `/admin` uses) and asks
   `POST /api/aihub/session` for one.
2. The session is only issued when the token is valid, the email is in
   `AIHUB_EMAILS` and the account is an admin right now. The cookie is
   HttpOnly, `SameSite=Strict`, limited to `/aihub`, valid 12 hours, and has
   `aud=aihub`, so it cannot be used against Zorken's own API.
3. Every request is checked again (cookie, email list, live admin access), then
   forwarded to the home PC with the shared `AIHUB_KEY`. Zorken's cookies and
   `Authorization` header are never forwarded. `/aihub/comfy/…` goes to
   ComfyUI, including its websocket.

The container reaches the home PC through Tailscale in userspace mode (no extra
Railway permissions); nothing on the home network is opened to the internet.

## Railway variables

| Variable | Example | Notes |
|---|---|---|
| `TS_AUTHKEY` | `tskey-auth-…` | Tailscale auth key, ideally tagged `tag:zorken` (no expiry). Starts Tailscale in the container. |
| `AIHUB_UPSTREAM` | `http://100.x.y.z:8090` | Home PC's Tailscale IP + hub port. |
| `AIHUB_COMFY_UPSTREAM` | `http://100.x.y.z:8188` | Defaults to the same host on port 8188. |
| `AIHUB_KEY` | (long random) | Must equal `remote.key` in the hub's `config.json`. |
| `AIHUB_EMAILS` | `jorgenthin@frid-iks.no` | Comma-separated. |
| `TS_HOSTNAME` | `zorken-railway` | Optional. |

`AIHUB_HTTP_PROXY` is set by `deploy/entrypoint.sh` when `TS_AUTHKEY` is set.
Tailscale state is kept on the volume in `/root/.helix/tailscale/`.

## Home PC

1. Install Tailscale and sign in.
2. Share the two ports inside the tailnet only:
   `tailscale serve --bg --http=8090 http://127.0.0.1:8090` and
   `tailscale serve --bg --http=8188 http://127.0.0.1:8188`.
3. Recommended tailnet ACL: `tag:zorken` may only reach the home PC on 8090 and 8188.

## Tests

`python -m pytest tests/test_aihub.py` runs a fake home PC (hub + ComfyUI) and
checks: gate page/401 without a cookie, refusal for other emails and non-admins,
the cookie is useless as a Zorken token, key added and identity headers
stripped, request bodies, streaming (SSE), ComfyUI HTTP and websocket, and that
removing an email locks out an existing cookie.
