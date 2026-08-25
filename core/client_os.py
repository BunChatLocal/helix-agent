"""Which machine the person in /chat is sitting at.

The browser already knows this. We pass a short allowlisted label to the
model so it does not ask "Windows, Mac, iPhone or Android?" when the answer
is in the User-Agent. The label never goes into the stored transcript.
"""

from __future__ import annotations

# What the wifi guide and the model should see. Anything else is dropped.
LABELS = ("Windows", "macOS", "iOS", "Android", "Linux", "ChromeOS")

_HINT = {
    "windows": "Windows",
    "win": "Windows",
    "macos": "macOS",
    "mac os": "macOS",
    "mac": "macOS",
    "ios": "iOS",
    "iphone": "iOS",
    "ipad": "iOS",
    "android": "Android",
    "linux": "Linux",
    "chromeos": "ChromeOS",
    "chrome os": "ChromeOS",
    "cros": "ChromeOS",
}

_DISPLAY = {
    "Windows": "a Windows PC",
    "macOS": "a Mac",
    "iOS": "an iPhone or iPad",
    "Android": "an Android device",
    "Linux": "a Linux computer",
    "ChromeOS": "a Chromebook",
}


def from_user_agent(ua: str) -> str:
    """Best-effort label from a User-Agent string, or empty."""
    if not ua:
        return ""
    if "Android" in ua:
        return "Android"
    if "iPhone" in ua or "iPad" in ua or "iPod" in ua:
        return "iOS"
    if "Windows" in ua:
        return "Windows"
    if "CrOS" in ua:
        return "ChromeOS"
    if "Mac OS" in ua or "Macintosh" in ua:
        return "macOS"
    if "Linux" in ua:
        return "Linux"
    return ""


def normalize(hint: str = "", user_agent: str = "") -> str:
    """Allowlisted OS from the client hint, else from the request User-Agent."""
    raw = (hint or "").strip()
    if raw in LABELS:
        return raw
    key = raw.lower()
    if len(key) <= 32 and key in _HINT:
        return _HINT[key]
    return from_user_agent(user_agent or "")


_ANSWER = {
    "Windows": "Windows",
    "macOS": "Mac",
    "iOS": "iPhone eller iPad",
    "Android": "Android",
    "Linux": "Linux",
    "ChromeOS": "Chromebook",
}


def prompt_note(label: str) -> str:
    """Marker the model sees. Empty when we do not know the device."""
    if label not in LABELS:
        return ""
    who = _DISPLAY[label]
    answer = _ANSWER[label]
    return (
        f"[DEVICE: {label}. Their browser is on {who}. "
        f"If they ask which operating system or device they are on, answer "
        f"«{answer}». Do not say you cannot see the device — the browser "
        "already told you, and that is not looking at their files. "
        "Do not ask which device unless they say they mean a different "
        "machine.]\n\n"
    )
