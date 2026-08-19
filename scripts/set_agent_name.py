"""Set the agent's display name in ~/.helix/config.json.

Rewrites only `agent_name`, leaving every other key byte-for-byte as it was —
`agent_id` in particular, which keys the session store and the log paths, so
changing it would orphan every existing conversation.

    python scripts/set_agent_name.py Zork
"""

import json
import pathlib
import sys

CONFIG = pathlib.Path.home() / ".helix" / "config.json"


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: set_agent_name.py <name>")
    name = sys.argv[1]

    data = json.loads(CONFIG.read_text(encoding="utf-8"))
    before = data.get("agent_name", "")
    data["agent_name"] = name
    CONFIG.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"agent_name: {before!r} -> {name!r}")
    print(f"agent_id left as {data.get('agent_id')!r} (sessions are keyed on it)")


if __name__ == "__main__":
    main()
