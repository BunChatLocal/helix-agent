"""Turn an Insights row into a draft verified-procedure stub.

The stub is a reminder to write the real steps — it is not itself a procedure.
Personal data is stripped before anything is appended to a skill file.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from core.skill_manager import read_skill, skill_path, validate_skill_id, write_skill

_EMAIL = re.compile(r"\b[\w.+-]+@[\w.-]+\.\w+\b")
_FNR = re.compile(r"\b\d{6}\s?\d{5}\b")
_PHONE = re.compile(r"\b(?:\+47\s?)?\d{8}\b")
_HEADING = re.compile(r"^#{1,6}\s+", re.M)


def strip_pii(text: str) -> str:
    """Remove emails, birth numbers and phone-shaped digits. Names are left;
    the operator still has to check before saving."""
    out = _EMAIL.sub("[e-post]", text or "")
    out = _FNR.sub("[id-nummer]", out)
    out = _PHONE.sub("[telefon]", out)
    return out.strip()


def _heading(title: str) -> str:
    clean = strip_pii(title)
    clean = _HEADING.sub("", clean)
    clean = re.sub(r"\s+", " ", clean).strip(" .")
    if not clean:
        clean = "Uten tittel"
    if len(clean) > 90:
        clean = clean[:87].rsplit(" ", 1)[0] + "…"
    return clean


def draft_block(title: str, preview: str = "", skill: str = "") -> str:
    heading = _heading(title)
    asked = strip_pii(preview or title)
    today = date.today().isoformat()
    return (
        f"\n### {heading}\n"
        f"**Gjelder:** (utkast fra Insights — fyll inn hvem)\n\n"
        f"**Spurt:** {asked}\n\n"
        f"**Slik gjør du:**\n"
        f"1. (fyll inn stegene fra en ekte sak — ikke gjett)\n\n"
        f"**Verifisert:** (utkast {today} — ikke ferdig)\n"
    )


def promote(
    workspace_path: str,
    skill_id: str,
    title: str,
    preview: str = "",
) -> dict:
    """Append a draft heading to the skill's SKILL.md. Returns the written skill."""
    sid = validate_skill_id(skill_id)
    path = skill_path(workspace_path, sid)
    if not path.exists():
        raise FileNotFoundError(sid)
    existing = Path(path).read_text(encoding="utf-8")
    block = draft_block(title, preview, sid)
    if heading_exists(existing, _heading(title)):
        return read_skill(workspace_path, sid) | {"already": True}
    write_skill(workspace_path, sid, existing.rstrip() + "\n" + block)
    result = read_skill(workspace_path, sid)
    result["already"] = False
    result["draft_heading"] = _heading(title)
    return result


def heading_exists(content: str, heading: str) -> bool:
    needle = heading.strip().lower()
    for line in content.splitlines():
        if line.startswith("### ") and line[4:].strip().lower() == needle:
            return True
    return False
