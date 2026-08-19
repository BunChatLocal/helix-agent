"""Readable, reusable subjects for the Insights view.

Skill ids (`frid-nettverk`) tell you which file to open. They do not tell you
what people were stuck on. This module turns an opening message into a short
Norwegian (or English) phrase, and reuses an existing phrase when a new
question is the same problem in different words.

The catalog lives in ``~/.helix/subjects.json`` so a label survives retention
deleting the conversation that created it. Matching is local and deterministic
— no model call — so two phrasings of the same problem stay in one bracket
without inventing a slightly different title each time.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

logger = logging.getLogger("helix.subjects")

CATALOG_PATH = Path.home() / ".helix" / "subjects.json"
_LOCK = threading.Lock()

_WORD = re.compile(r"[a-zæøåäöü0-9][a-zæøåäöü0-9\-]{1,}", re.I)
_SPACE = re.compile(r"\s+")
_GREETING = re.compile(
    r"^(?:hei|heisann|hallo|hello|hi|hoi|yo|tja|god\s+(?:morgen|dag|kveld)|ok|okay|"
    r"takk|thanks)\b[\s,.!:;–-]*",
    re.I,
)

_STOP = {
    "jeg", "meg", "min", "mitt", "mine", "du", "deg", "din", "ditt", "dine",
    "det", "den", "de", "dem", "vi", "oss", "han", "hun", "seg",
    "er", "var", "har", "hadde", "kan", "kunne", "skal", "skulle", "vil",
    "ville", "må", "får", "gjør", "gjøre", "blir", "ble", "være",
    "ikke", "ingen", "noe", "noen", "alt", "alle", "hva", "hvorfor",
    "når", "hvor", "hvem", "som", "at", "og", "eller", "men", "for", "til",
    "fra", "med", "uten", "på", "i", "av", "om", "over", "under", "etter",
    "en", "et", "ei", "the", "a", "an", "my", "me", "is", "are", "was",
    "have", "has", "can", "cannot", "cant", "wont", "will", "would", "do",
    "does", "did", "not", "no", "what", "why", "when", "where", "who",
    "and", "or", "but", "to", "from", "with", "on", "in", "of", "about",
    "hei", "hallo", "takk", "hjelp", "help", "please", "problem", "problemer",
    "funker", "fungerer", "virker",
}

# "hvordan" is how people ask; it should not become the subject, but stripping
# it from the displayed phrase makes "Hvordan logger jeg på FRID-nettverket"
# worse. It is only a stopword for matching.
_MATCH_STOP = _STOP | {"hvordan", "how"}

_MAX_LABEL = 90
_MAX_EXAMPLES = 8


def labelize(text: str) -> str:
    """A readable heading from the opening message. Empty if there is no question."""
    raw = _SPACE.sub(" ", (text or "").strip())
    if not raw:
        return ""
    stripped = _GREETING.sub("", raw).strip(" .,:;–-")
    phrase = stripped or raw
    phrase = _SPACE.sub(" ", phrase).strip()
    if not phrase or phrase.lower() in {"hei", "hallo", "hello", "hi"}:
        return ""
    if len(phrase) > _MAX_LABEL:
        cut = phrase[:_MAX_LABEL].rsplit(" ", 1)[0]
        phrase = (cut or phrase[:_MAX_LABEL]).rstrip(".,;:") + "…"
    return phrase[0].upper() + phrase[1:] if phrase else ""


def _tokens(text: str) -> set[str]:
    out = set()
    for word in _WORD.findall((text or "").lower()):
        if word in _MATCH_STOP or word.isdigit() or len(word) < 3:
            continue
        out.add(word)
        if "-" in word:
            out.update(
                p for p in word.split("-")
                if len(p) >= 3 and p not in _MATCH_STOP
            )
    return out


def _score(msg_tokens: set[str], entry_tokens: set[str]) -> float:
    if not msg_tokens or not entry_tokens:
        return 0.0
    inter = msg_tokens & entry_tokens
    if not inter:
        return 0.0
    # Overlap coefficient: a short "wifi" still joins a longer wifi question.
    return len(inter) / min(len(msg_tokens), len(entry_tokens))


def _strong(token: str) -> bool:
    return len(token) >= 5 or token in {
        "wifi", "mfa", "vis", "pc", "mac", "pin", "sms", "vpn",
    }


def _is_match(msg_tokens: set[str], entry_tokens: set[str]) -> bool:
    inter = msg_tokens & entry_tokens
    if not inter:
        return False
    coef = _score(msg_tokens, entry_tokens)
    if len(inter) >= 2 and coef >= 0.4:
        return True
    if len(inter) == 1:
        only = next(iter(inter))
        return _strong(only) and coef >= 0.5
    return False


def _load(path: Path) -> list[dict]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data.get("subjects") if isinstance(data, dict) else data
        return list(entries) if isinstance(entries, list) else []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read subjects catalog: {e}")
        return []


def _save(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"subjects": entries}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def _entry_tokens(entry: dict) -> set[str]:
    parts = [entry.get("label") or ""]
    parts.extend(entry.get("examples") or [])
    tokens = set()
    for part in parts:
        tokens |= _tokens(part)
    return tokens


def match_subject(text: str, catalog: list[dict]) -> dict | None:
    """The existing catalog row this question belongs to, or None."""
    msg_tokens = _tokens(text)
    if not msg_tokens:
        return None
    best = None
    best_score = 0.0
    for entry in catalog:
        et = _entry_tokens(entry)
        if not _is_match(msg_tokens, et):
            continue
        score = _score(msg_tokens, et)
        if score > best_score:
            best, best_score = entry, score
    return best


def remember_subject(text: str, skill: str = "", *, path: Path | None = None) -> str:
    """Label this opening message, reusing a catalog entry when it is the same problem.

    Persists the catalog. Returns "" when the message has no subject (a greeting).
    """
    label = labelize(text)
    if not label:
        return ""
    dest = path or CATALOG_PATH
    with _LOCK:
        catalog = _load(dest)
        existing = match_subject(text, catalog)
        if existing:
            examples = list(existing.get("examples") or [])
            preview = _SPACE.sub(" ", text.strip())[:120]
            if preview and preview not in examples:
                examples.append(preview)
                existing["examples"] = examples[-_MAX_EXAMPLES:]
            if skill and not existing.get("skill"):
                existing["skill"] = skill
            _save(dest, catalog)
            return existing["label"]
        catalog.append({
            "label": label,
            "skill": skill or "",
            "examples": [_SPACE.sub(" ", text.strip())[:120]],
        })
        _save(dest, catalog)
        return label


def resolve_subject(
    text: str,
    stored: str = "",
    catalog: list[dict] | None = None,
) -> str:
    """Subject for an audit row, without writing.

    Used when drawing Insights: stored labels win; otherwise match the
    in-memory catalog (oldest conversations first, so the first phrasing
    becomes the bracket later ones join).
    """
    if stored:
        return stored
    catalog = catalog if catalog is not None else []
    existing = match_subject(text, catalog)
    if existing:
        return existing["label"]
    return labelize(text)


def count_subjects(events: list[dict], *, path: Path | None = None) -> dict[str, int]:
    """Count conversations per readable subject, oldest first so later
    phrasings join the first wording of the same problem.

    Does not write. ``remember_subject`` is what persists a new bracket.
    """
    catalog = _load(path or CATALOG_PATH)
    counts: dict[str, int] = {}
    for e in sorted(events, key=lambda ev: ev.get("ts") or 0):
        text = e.get("content_preview") or ""
        stored = (e.get("subject") or "").strip()
        if stored:
            label = stored
        else:
            hit = match_subject(text, catalog)
            label = hit["label"] if hit else labelize(text)
        if not label:
            continue
        counts[label] = counts.get(label, 0) + 1
        preview = _SPACE.sub(" ", text.strip())[:120]
        hit = match_subject(text, catalog)
        if hit is None:
            catalog.append({
                "label": label,
                "skill": e.get("topic") or "",
                "examples": [preview] if preview else [],
            })
        elif preview and preview not in (hit.get("examples") or []):
            examples = list(hit.get("examples") or [])
            examples.append(preview)
            hit["examples"] = examples[-_MAX_EXAMPLES:]
    return counts
