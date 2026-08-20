"""Live workspace vs the git template.

Skills and Files in the admin UI edit ``~/.helix/workspace``. The copy that
travels with the repo is ``workspace-template/``. A good answer written on this
machine can vanish on the next setup elsewhere unless someone copies it back.
This module only reports the gap — it does not overwrite anything by itself.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional

from core.config import get_config

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = REPO_ROOT / "workspace-template"

# Instance-specific files: expected to differ, not a knowledge-base problem.
_SKIP_NAMES = {
    "USER.md",
    "IDENTITY.md",
    "MEMORY.md",
}
_SKIP_DIRS = {"memory"}


def _workspace() -> Path:
    return Path(get_config().workspace_path)


def _rel_files(root: Path) -> dict[str, Path]:
    out = {}
    if not root.exists():
        return out
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        parts = Path(rel).parts
        if parts and parts[0] in _SKIP_DIRS:
            continue
        if path.name in _SKIP_NAMES:
            continue
        out[rel] = path
    return out


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def drift_report(
    workspace: Optional[Path] = None,
    template: Optional[Path] = None,
) -> dict:
    """Compare tracked template files with the live workspace."""
    live_root = Path(workspace) if workspace else _workspace()
    tmpl_root = Path(template) if template else TEMPLATE_DIR
    live = _rel_files(live_root)
    tmpl = _rel_files(tmpl_root)

    diverged = []
    missing_in_live = []
    only_in_live = []

    for rel, tmpl_path in sorted(tmpl.items()):
        live_path = live.get(rel)
        if live_path is None:
            missing_in_live.append(rel)
            continue
        try:
            if _digest(tmpl_path) != _digest(live_path):
                diverged.append(rel)
        except OSError:
            diverged.append(rel)

    for rel in sorted(live):
        if rel not in tmpl and rel.startswith("skills/"):
            only_in_live.append(rel)

    return {
        "template": str(tmpl_root),
        "workspace": str(live_root),
        "diverged": diverged,
        "missing_in_live": missing_in_live,
        "only_in_live": only_in_live,
        "ok": not (diverged or missing_in_live),
    }
