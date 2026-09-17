"""Copy ~/.helix into deploy/helix-home for the Railway image.

Decrypts the Windows-bound secrets vault into secrets.json so Linux can
re-encrypt it on first boot. Does not print secret values.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SRC = Path.home() / ".helix"
DEST = ROOT / "deploy" / "helix-home"
SKIP_DIRS = set()
SKIP_NAMES = {"helix.pid", "secrets.enc", ".salt"}


def _copy_tree() -> None:
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    for item in SRC.iterdir():
        if item.name in SKIP_NAMES or item.name in SKIP_DIRS:
            continue
        if item.name.startswith(".claude.json.tmp."):
            continue
        target = DEST / item.name
        if item.is_dir():
            shutil.copytree(
                item, target,
                ignore=shutil.ignore_patterns("*.tmp.*", "*.pid", "helix.console.*"),
            )
        else:
            shutil.copy2(item, target)


def _dump_secrets() -> int:
    from security.secrets import get_secret, list_keys
    keys = list_keys()
    payload = {}
    for key in keys:
        value = get_secret(key)
        if isinstance(value, str) and value:
            payload[key] = value
    (DEST / "secrets.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return len(payload)


def _rewrite_config() -> None:
    path = DEST / "config.json"
    if not path.is_file():
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["workspace_path"] = "/root/.helix/workspace"
    web = cfg.setdefault("web", {})
    web["host"] = "0.0.0.0"
    web["dev_login"] = True
    entra = cfg.setdefault("entra", {})
    entra["enabled"] = False
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    if not SRC.is_dir():
        sys.exit(f"no Helix home at {SRC}")
    _copy_tree()
    n = _dump_secrets()
    _rewrite_config()
    restore = DEST / ".restore-local"
    if restore.exists():
        restore.unlink()
        print("removed .restore-local (entrypoint will not overwrite a live volume)")
    print(f"packed {DEST}")
    print(f"secret keys written: {n}")


if __name__ == "__main__":
    main()
