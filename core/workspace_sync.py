"""Live workspace vs the git template.

Skills and Files in the admin UI edit ``~/.helix/workspace``. The copy that
travels with the repo is ``workspace-template/``. A good answer written on this
machine can vanish on the next setup elsewhere unless someone copies it back.
`copy_skill_to_template` is the explicit write: live skill + cited screenshots
into ``workspace-template/``, which is what the next machine gets.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Optional

from core.config import get_config
from core.skill_manager import validate_skill_id

_ASSET_REF = re.compile(r"asset:([A-Za-z0-9._-]{1,80})")

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


def _safe_under(root: Path, candidate: Path) -> Path:
    resolved = candidate.resolve()
    resolved.relative_to(root.resolve())
    return resolved


def copy_skill_to_template(
    skill_id: str,
    workspace: Optional[Path] = None,
    template: Optional[Path] = None,
) -> dict:
    """Copy a live skill (and the screenshots it cites) into workspace-template.

    Overwrites the matching template files. That is the point: the live
    workspace is where Insights drafts land, the template is what the next
    machine gets.
    """
    sid = validate_skill_id(skill_id)
    live_root = Path(workspace) if workspace else _workspace()
    tmpl_root = Path(template) if template else TEMPLATE_DIR
    src_dir = _safe_under(live_root / "skills", live_root / "skills" / sid)
    if not src_dir.is_dir():
        raise FileNotFoundError(sid)

    dst_dir = tmpl_root / "skills" / sid
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for item in sorted(src_dir.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_file() or item.name.startswith("."):
            continue
        shutil.copy2(item, dst_dir / item.name)
        copied.append(f"skills/{sid}/{item.name}")

    names: set[str] = set()
    for md in src_dir.glob("*.md"):
        try:
            names.update(_ASSET_REF.findall(md.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue

    live_assets = live_root / "assets"
    tmpl_assets = tmpl_root / "assets"
    tmpl_assets.mkdir(parents=True, exist_ok=True)
    assets = []
    missing_assets = []
    for name in sorted(names):
        src = live_assets / name
        try:
            src = _safe_under(live_assets, src)
        except ValueError:
            missing_assets.append(name)
            continue
        if not src.is_file():
            missing_assets.append(name)
            continue
        shutil.copy2(src, tmpl_assets / name)
        assets.append(name)

    return {
        "skill": sid,
        "copied": copied,
        "assets": assets,
        "missing_assets": missing_assets,
    }
