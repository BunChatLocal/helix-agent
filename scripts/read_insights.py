"""Fetch /api/insights from the running server and print the coverage table.

Mints its own admin token rather than using the web password, which lives in the
encrypted secret store.

    python scripts/read_insights.py [days]
"""

import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.config import get_config  # noqa: E402
from web.app import _create_token  # noqa: E402


def main() -> None:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    port = get_config().web.admin_port
    token = _create_token(sub="script", role="admin")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/insights?days={days}",
        headers={"Authorization": f"Bearer {token}"},
    )
    import json
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)

    t = data["totals"]
    print(f"last {data['days']} days: {t['conversations']} conversations, "
          f"{t['people']} people, {t['refusals']} refusals, "
          f"{t['undocumented']} undocumented")
    print(f"unclassified: {data['unclassified']}\n")

    print(f"{'skill':<20} {'asked':>5} {'proc':>4}  file")
    for row in data["coverage"]:
        state = "documented" if row["documented"] else "NOTHING DOCUMENTED"
        flag = "  <-- write next" if row["undocumented_demand"] else ""
        print(f"{row['skill']:<20} {row['asked']:>5} {row['procedures']:>4}  "
              f"{row.get('file', '')}  {state}{flag}")

    if data["by_topic"]:
        print("\nasked about:")
        for row in data["by_topic"]:
            print(f"  {row['key']:<20} {row['count']}")


if __name__ == "__main__":
    main()
