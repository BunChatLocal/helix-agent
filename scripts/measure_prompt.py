"""Show how large each tier's system prompt is, and what it is made of.

Run from the repo root:  python scripts/measure_prompt.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import TIER_STAFF, TIER_STUDENT, get_config  # noqa: E402
from core.context_engine import bootstrap_files, build_system_prompt  # noqa: E402


def main() -> None:
    workspace = Path(get_config().workspace_path)
    for tier in (TIER_STAFF, TIER_STUDENT):
        print(f"\n=== {tier} ===")
        total = 0
        for name in bootstrap_files(tier):
            fp = workspace / name
            if not fp.exists():
                print(f"  {name:45} MISSING")
                continue
            n = len(fp.read_text(encoding="utf-8", errors="replace"))
            total += n
            print(f"  {name:45} {n:>7}")
        prompt = build_system_prompt(True, tier)
        print(f"  {'files subtotal':45} {total:>7}")
        print(f"  {'full prompt (with header/scope note)':45} {len(prompt):>7}")


if __name__ == "__main__":
    main()
