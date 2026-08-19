"""What the agent is asked about, and what it has actually documented.

This exists to answer the one question the audit records could not: where are
people asking about a domain whose skill file holds no verified, org-specific
knowledge? That is where the agent is reasoning from general knowledge, and where
it is most likely to be confidently wrong — which `AGENTS.md` calls worse than no
answer at all. Demand and coverage are each easy to see alone and useless apart:
an empty skill nobody asks about can wait, and a busy domain with nothing written
down is the next thing to document.

Both measures here are heuristics and neither enforces anything. A wrong answer
costs a mislabelled row in an admin report.

**Topic** derives its vocabulary from the skill frontmatter, so adding a skill
makes it classifiable without editing this file. The frontmatter is English while
most questions arrive in Norwegian, so `ALIASES` supplies the words people
actually type — "treg", "skriver", "passord", "lønn".

**Coverage** asks whether a skill contains anything specific to this
organisation. Counting `###` entries under a "Verified" heading is not sufficient
on its own: `frid-nettverk` is the best-documented file in the workspace and has
none, because its entire body is verified content from the onboarding portal. So
a skill counts as documented if it has verified entries *or* org-specific
anchors — a real hostname, or a screenshot of a real screen.
"""

import logging
import re
from pathlib import Path
from typing import Optional

from core.config import TIER_STUDENT, get_config

logger = logging.getLogger("helix.knowledge")

_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_DESCRIPTION = re.compile(r"^description:\s*(.+)$", re.M | re.I)
_HEADING = re.compile(r"^(#{2,3})\s+(.+?)\s*$", re.M)
_VERIFIED_SECTION = re.compile(r"^verified\b", re.I)
_ASSET_REF = re.compile(r"asset:([A-Za-z0-9._-]+)")
# A hostname under one of the organisation's own domains. The counties are here
# because pupil-facing procedures point at brukerkonto.<county>.no.
_ORG_HOST = re.compile(r"\b(?:[a-z0-9-]+\.)*(?:frid-iks|frid|afk|ofk|bfk)\.no\b", re.I)
_WORD = re.compile(r"[a-zæøåäöü0-9][a-zæøåäöü0-9\-]{2,}")
# Norwegian inflects heavily — "aktivere", "aktiverer", "aktivering", "aktiveringen"
# are one question. Rather than enumerate the forms, a term of at least this length
# also matches any token it is a prefix of. Short terms stay exact, because "nett"
# would otherwise claim "nettside" and "pc" would claim half the dictionary.
_STEM_MIN = 5

# Frontmatter descriptions are prose, so strip the words that describe every
# skill equally rather than distinguishing one from another.
_DESCRIPTION_FILLER = {
    "and", "the", "for", "with", "from", "into", "only", "any", "all", "not",
    "problems", "problem", "issues", "issue", "troubleshooting", "help",
    "where", "look", "what", "when", "who", "how", "advisory", "access",
    "getting", "onto", "per", "its", "own", "side", "client", "centres",
    "centre", "admin", "user", "users", "device", "devices", "school",
}

# The words people actually type, which the English frontmatter does not cover.
# Keyed by skill id. Adding a term here is the cheapest way to fix a
# misclassification you notice in the Insights view.
#
# Write real Norwegian, with æøå. These are matched against what people type, so
# an ASCII spelling like "loenn" or "fravaer" is a term that can never match.
# Prefer the stem: "aktiver" covers aktivere/aktiverer/aktivering, and a longer
# inflection alongside it is redundant (see _collapse_stems).
#
# Beware Norwegian compounds when choosing a stem. "personal" was here for HR and
# matched "personalrommet" — the staff room — routing a printer fault at payroll.
ALIASES: dict[str, tuple[str, ...]] = {
    "windows-endpoint": (
        "pc", "pcen", "laptop", "maskin", "datamaskin", "treg", "treig",
        "henger", "fryser", "krasjer", "skriver", "printer", "utskrift",
        "skjerm", "oppstart", "starter", "batteri", "lader", "driver",
        "mus", "tastatur", "bluetooth", "usb", "lyd", "kamera",
        "slow", "printing", "boot", "crash",
    ),
    "entra-azure": (
        "passord", "innlogging", "logge", "konto", "sperret", "låst",
        "utestengt", "tofaktor", "totrinns", "lisens", "tilgang", "gruppe",
        "brukerkonto", "locked", "licence", "license", "signin", "password",
    ),
    "m365-office": (
        "outlook", "mail", "epost", "post", "teams", "onedrive", "sharepoint",
        "excel", "word", "powerpoint", "kalender", "møte", "innkalling",
        "vedlegg", "deling", "delt", "innboks", "sende", "mottar", "signatur",
        "email", "inbox", "calendar", "attachment",
    ),
    "frid-nettverk": (
        "wifi", "wifien", "nett", "nettet", "nettverk", "internett", "trådløs",
        "sertifikat", "aktiver", "onboard", "koble", "tilkobling",
        "eduroam", "authenticator", "mfa", "network", "certificate",
    ),
    "visma-inschool": (
        "vis", "inschool", "timeplan", "karakter", "fravær", "elev",
        "klasse", "skolesystem", "vurdering", "faglærer", "kontaktlærer",
        "timetable", "grades",
    ),
    "visma-enterprise": (
        "lønn", "egenmelding", "ansatt", "ferie", "refusjon", "sykemelding",
        "sykmelding", "timeregistrering", "reiseregning", "payroll", "salary",
        "absence",
    ),
}


def _tokens(text: str) -> set[str]:
    """Words in `text`, with hyphenated compounds also split into their parts.

    "SharePoint-området" and "PC-en" are single tokens to a word regex, which
    would hide the only word that identifies the domain.
    """
    tokens = set()
    for word in _WORD.findall(text.lower()):
        tokens.add(word)
        if "-" in word:
            tokens.update(part for part in word.split("-") if len(part) > 1)
    return tokens


def _matches(terms: set[str], tokens: set[str]) -> int:
    """How many of `terms` appear in `tokens`, allowing stem-prefix matches."""
    hits = 0
    for term in terms:
        if term in tokens:
            hits += 1
        elif len(term) >= _STEM_MIN and any(t.startswith(term) for t in tokens):
            hits += 1
    return hits


def _collapse_stems(terms: set[str]) -> set[str]:
    """Drop inflections a shorter stem in the same set already covers.

    Scoring counts matched terms, so holding both "konto" and "kontoen" scores one
    word in the question twice — enough for a domain to outvote another on spelling
    alone. Keeping only the stem makes the score a count of distinct concepts.
    """
    kept: set[str] = set()
    for term in sorted(terms, key=len):
        if any(len(s) >= _STEM_MIN and term != s and term.startswith(s) for s in kept):
            continue
        kept.add(term)
    return kept


def _workspace(workspace: Optional[Path] = None) -> Path:
    return Path(workspace) if workspace else Path(get_config().workspace_path)


def _skill_texts(workspace: Optional[Path] = None) -> dict[str, str]:
    """Every `skills/<id>/SKILL.md` in the workspace, by id."""
    root = _workspace(workspace) / "skills"
    if not root.is_dir():
        return {}
    out = {}
    for child in sorted(root.iterdir()):
        skill = child / "SKILL.md"
        if not skill.is_file():
            continue
        try:
            out[child.name] = skill.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning(f"Could not read {skill}: {exc}")
    return out


def _description(text: str) -> str:
    match = _FRONTMATTER.match(text)
    if not match:
        return ""
    desc = _DESCRIPTION.search(match.group(1))
    return desc.group(1).strip() if desc else ""


def vocabulary(workspace: Optional[Path] = None) -> dict[str, set[str]]:
    """Keywords per skill: its id, its frontmatter description, and its aliases."""
    vocab: dict[str, set[str]] = {}
    for skill_id, text in _skill_texts(workspace).items():
        words = {w for w in skill_id.lower().split("-") if len(w) > 2}
        for word in _tokens(_description(text)):
            if word not in _DESCRIPTION_FILLER:
                words.add(word)
        words.update(ALIASES.get(skill_id, ()))
        vocab[skill_id] = _collapse_stems(words)
    return vocab


def _pupil_skills(workspace: Optional[Path] = None) -> set[str]:
    """The skills a pupil's prompt carries, used only to break a scoring tie.

    A pupil asking about MFA means the activation guide; a staff member asking
    means the reset procedure. Same word, different file.
    """
    try:
        from core.context_engine import PUPIL_KNOWLEDGE
    except Exception:
        return set()
    return {p.split("/")[1] for p in PUPIL_KNOWLEDGE if p.count("/") >= 2}


def classify_topic(
    text: str,
    tier: str = "",
    workspace: Optional[Path] = None,
) -> Optional[str]:
    """Which skill this question is about, or None if nothing matched.

    Scored on how many distinct keywords match, so a longer question about
    Outlook beats a passing mention of a password.
    """
    if not text:
        return None
    tokens = _tokens(text)
    if not tokens:
        return None

    vocab = vocabulary(workspace)
    scores = {sid: _matches(terms, tokens) for sid, terms in vocab.items()}
    best = max(scores.values()) if scores else 0
    if best == 0:
        return None

    winners = sorted(sid for sid, n in scores.items() if n == best)
    if len(winners) == 1:
        return winners[0]
    if tier == TIER_STUDENT:
        pupil = [sid for sid in winners if sid in _pupil_skills(workspace)]
        if pupil:
            return pupil[0]
    return winners[0]


def _count_verified(text: str) -> int:
    """`###` entries under a "Verified" heading, ignoring template stubs."""
    count = 0
    in_verified = False
    for level, title in _HEADING.findall(text):
        if len(level) == 2:
            in_verified = bool(_VERIFIED_SECTION.match(title.strip()))
            continue
        # A stub still carries its angle brackets: "### <Short problem title>".
        if in_verified and "<" not in title and ">" not in title:
            count += 1
    return count


def skill_coverage(workspace: Optional[Path] = None) -> dict[str, dict]:
    """Per skill: how much verified, org-specific knowledge it actually holds."""
    out = {}
    for skill_id, text in _skill_texts(workspace).items():
        procedures = _count_verified(text)
        anchors = len(set(_ASSET_REF.findall(text))) + len(
            {h.lower() for h in _ORG_HOST.findall(text)}
        )
        out[skill_id] = {
            "procedures": procedures,
            "anchors": anchors,
            "chars": len(text),
            "documented": bool(procedures or anchors),
        }
    return out


def coverage_report(
    topic_counts: dict[str, int],
    workspace: Optional[Path] = None,
) -> list[dict]:
    """Demand joined to coverage, busiest first.

    Includes skills nobody asked about, so an empty file is visible before it is
    urgent rather than after.
    """
    coverage = skill_coverage(workspace)
    rows = []
    for skill_id, facts in coverage.items():
        asked = topic_counts.get(skill_id, 0)
        rows.append({
            "skill": skill_id,
            "file": f"skills/{skill_id}/SKILL.md",
            "asked": asked,
            "procedures": facts["procedures"],
            "anchors": facts["anchors"],
            "documented": facts["documented"],
            # The number worth acting on: questions arriving somewhere with
            # nothing written down.
            "undocumented_demand": asked if not facts["documented"] else 0,
        })
    rows.sort(key=lambda r: (-r["undocumented_demand"], -r["asked"], r["skill"]))
    return rows
