"""
Helix Skill Manager
Workspace-backed helper for user-defined skills stored at skills/<id>/SKILL.md.
"""

import os
import re
from pathlib import Path


SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def skills_root(workspace_path: str) -> Path:
    root = Path(workspace_path) / SKILLS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def validate_skill_id(skill_id: str) -> str:
    normalized = (skill_id or "").strip().lower()
    if not _SKILL_ID_RE.match(normalized):
        raise ValueError(
            "Invalid skill id: use lowercase letters, numbers, dashes, or underscores"
        )
    return normalized


def slugify_skill_name(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", (name or "").strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-_")
    return validate_skill_id(normalized or "skill")


def _skill_dir(workspace_path: str, skill_id: str) -> Path:
    root = skills_root(workspace_path).resolve()
    resolved = (root / validate_skill_id(skill_id)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError("Skill path escapes skills directory")
    return resolved


def skill_path(workspace_path: str, skill_id: str) -> Path:
    return _skill_dir(workspace_path, skill_id) / SKILL_FILE


def _extract_title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _extract_description(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        return stripped[:140]
    return ""


def list_skills(workspace_path: str) -> list[dict]:
    root = skills_root(workspace_path)
    items = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        skill_file = entry / SKILL_FILE
        if not skill_file.exists():
            continue
        content = skill_file.read_text(encoding="utf-8", errors="replace")
        items.append(
            {
                "id": entry.name,
                "name": _extract_title(content, entry.name),
                "path": f"{SKILLS_DIR}/{entry.name}/{SKILL_FILE}",
                "description": _extract_description(content),
            }
        )
    return items


def read_skill(workspace_path: str, skill_id: str) -> dict:
    path = skill_path(workspace_path, skill_id)
    if not path.exists():
        raise FileNotFoundError("Skill not found")
    content = path.read_text(encoding="utf-8")
    normalized = validate_skill_id(skill_id)
    return {
        "id": normalized,
        "name": _extract_title(content, normalized),
        "path": f"{SKILLS_DIR}/{normalized}/{SKILL_FILE}",
        "description": _extract_description(content),
        "content": content,
    }


def scaffold_skill_content(name: str, description: str = "") -> str:
    desc = description.strip() or "Reusable workflow instructions for a Helix agent."
    return (
        f"# {name.strip()}\n\n"
        f"{desc}\n\n"
        "## When To Use\n\n"
        "- Describe the kinds of requests or situations where this skill helps.\n\n"
        "## Instructions\n\n"
        "- Add the step-by-step workflow or guidance the agent should follow.\n"
        "- Keep it specific and actionable.\n\n"
        "## Notes\n\n"
        "- Add constraints, caveats, references, or examples here.\n"
    )


def create_skill(workspace_path: str, name: str, description: str = "", content: str = "") -> dict:
    skill_id = slugify_skill_name(name)
    path = skill_path(workspace_path, skill_id)
    if path.exists():
        raise FileExistsError("Skill already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    body = content if content.strip() else scaffold_skill_content(name=name.strip(), description=description)
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return read_skill(workspace_path, skill_id)


def write_skill(workspace_path: str, skill_id: str, content: str) -> dict:
    path = skill_path(workspace_path, skill_id)
    if not path.exists():
        raise FileNotFoundError("Skill not found")
    path.write_text(content, encoding="utf-8")
    return read_skill(workspace_path, skill_id)
