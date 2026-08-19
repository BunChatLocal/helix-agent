"""Store CLAUDE_CODE_OAUTH_TOKEN from a single pasted line.

    .\\.venv\\Scripts\\python.exe scripts\\set_claude_oauth_token.py

Paste the token at the prompt and press Enter. Prints only length and prefix.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from security.secrets import get_secret, set_secret  # noqa: E402


def main() -> None:
    try:
        raw = input("paste token: ")
    except EOFError:
        sys.exit("no input — run this in a real terminal, then paste the token")
    token = raw.strip().strip('"').strip("'")
    if not token:
        sys.exit("empty — nothing was stored")
    if not token.startswith("sk-ant-oat01-"):
        sys.exit(
            f"refused — value starts with {token[:12]!r}, expected sk-ant-oat01-"
        )
    if any(c.isspace() for c in token):
        sys.exit("refused — token contains a space")
    set_secret("CLAUDE_CODE_OAUTH_TOKEN", token)
    stored = get_secret("CLAUDE_CODE_OAUTH_TOKEN") or ""
    if stored != token:
        sys.exit("stored value did not match what was pasted")
    dest = Path.home() / ".helix" / "secrets.enc"
    mtime = dest.stat().st_mtime if dest.exists() else 0
    from datetime import datetime
    when = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S") if mtime else "missing"
    print(f"stored {len(stored)} chars, starts with {stored[:12]}")
    print(f"file {dest} mtime {when} size {dest.stat().st_size if dest.exists() else 0}")


if __name__ == "__main__":
    main()
