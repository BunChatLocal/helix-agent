"""
Helix Web Renderer
Fetches a page with a real browser so JavaScript-rendered content is visible.

Why this exists: many internal guide sites are single-page apps. A plain HTTP
fetch returns an empty shell, and content addressed by a URL fragment
(``/guides#aktivering-ny-konto``) never reaches the server at all — fragments
are client-side only. Rendering with a browser is the only way to read them.

Uses the Edge/Chrome already installed on the machine rather than downloading
a browser, so the footprint is just the Playwright package.
"""

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger("helix.web_render")

MAX_CHARS = 60_000
DEFAULT_TIMEOUT_MS = 45_000

# Browser channels to try, in order. All use an already-installed browser.
_CHANNELS = ("msedge", "chrome", None)


class RenderError(RuntimeError):
    """Raised when a page cannot be rendered."""


def _is_public_host(host: str) -> bool:
    """Reject anything that resolves to a private, loopback or link-local address.

    This renderer fetches URLs on behalf of a chat user, from inside a municipal
    network. Without this check it would be a server-side request forgery tool:
    someone could point it at an internal admin panel, a router, or a cloud
    metadata endpoint and read the response through the agent.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise RenderError(f"Could not resolve host: {host}") from exc

    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast):
            return False
    return True


def validate_url(url: str) -> str:
    """Validate and normalise a URL before rendering. Raises RenderError."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise RenderError("Only http:// and https:// addresses can be opened.")
    if not parsed.hostname:
        raise RenderError("That does not look like a web address.")
    if not _is_public_host(parsed.hostname):
        raise RenderError(
            "That address is on a private or internal network, which this tool "
            "will not open."
        )
    return parsed.geturl()


async def render_page(url: str, timeout_ms: int = DEFAULT_TIMEOUT_MS) -> dict:
    """Render *url* in a headless browser and return its visible text.

    Returns {"url", "title", "text"}. Raises RenderError on failure.
    """
    url = validate_url(url)

    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RenderError(
            "The page renderer is not installed. Run: "
            "pip install playwright"
        ) from exc

    last_error = None
    async with async_playwright() as p:
        for channel in _CHANNELS:
            try:
                kwargs = {"headless": True}
                if channel:
                    kwargs["channel"] = channel
                browser = await p.chromium.launch(**kwargs)
            except Exception as exc:
                last_error = exc
                continue

            try:
                context = await browser.new_context(
                    locale="nb-NO",
                    # Don't carry or store any identity — pages are read
                    # anonymously, exactly as a logged-out visitor sees them.
                    java_script_enabled=True,
                )
                page = await context.new_page()
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                # SPAs often paint after the network settles.
                await page.wait_for_timeout(1200)
                title = await page.title()
                # Prefer the main content region when the page marks one.
                text = ""
                for sel in ("main", "[role=main]", "#main-content", "body"):
                    try:
                        if await page.locator(sel).count():
                            text = await page.locator(sel).first.inner_text()
                            if text and text.strip():
                                break
                    except Exception:
                        continue
                return {
                    "url": url,
                    "title": (title or "").strip(),
                    "text": text.strip()[:MAX_CHARS],
                }
            except Exception as exc:
                last_error = exc
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    raise RenderError(
        f"Could not open the page. {type(last_error).__name__}: {last_error}"
        if last_error else "Could not open the page."
    )
