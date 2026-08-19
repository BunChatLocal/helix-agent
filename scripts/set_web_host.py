"""Set a single key under web in ~/.helix/config.json.

    python scripts/set_web_host.py 127.0.0.1
"""

import json
import pathlib
import sys

CONFIG = pathlib.Path.home() / ".helix" / "config.json"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: set_web_host.py <host>")
    host = sys.argv[1]
    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    web = data.setdefault("web", {})
    before = web.get("host", "")
    web["host"] = host
    CONFIG.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"web.host: {before!r} -> {host!r}")


if __name__ == "__main__":
    main()
