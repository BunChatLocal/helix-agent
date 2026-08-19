"""
Helix Context Engine
Builds the system prompt for each agent turn.
- Loads bootstrap files (AGENTS.md, SOUL.md, etc.) as stable cacheable prefix
- Caps each file at 20k chars, total bootstrap at 150k chars
"""

import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from core.config import get_config, TIER_STAFF, TIER_STUDENT

logger = logging.getLogger("helix.context")

# Loaded for everyone: who the agent is, and the rules that hold in every
# conversation. Both files are deliberately free of anything that assumes tools, a
# knowledge base or an audience — the method for working a case lives in
# OPERATIONS.md, on the staff side. STUDENT.md used to spend a section cancelling
# the operator material these two carried, and negation is weaker than absence:
# the same reasoning as STUDENT_TURN_MARKER below.
SHARED_BOOTSTRAP = [
    "AGENTS.md",
    "SOUL.md",
]

# Layered on top, per tier — see ROLES.md. Staff keep the operator-facing
# material. A pupil has no use for the working method in OPERATIONS.md (which
# routes to skills they cannot open and tells the agent to write findings back), a
# description of the operator (USER.md), the heartbeat and memory machinery, or the
# diagnostics list in TOOLS.md, which would only invite the agent to hand a
# sixteen-year-old a PowerShell command they have no rights to run.
#
# The tier file is where the scope rules live, so a missing one is not a cosmetic
# problem: without STUDENT.md a pupil gets no restriction at all. Absence is
# logged loudly below.
TIER_BOOTSTRAP = {
    TIER_STAFF: [
        "OPERATIONS.md",
        "IDENTITY.md",
        "USER.md",
        "HEARTBEAT.md",
        "MEMORY.md",
        "TOOLS.md",
        "STAFF.md",
    ],
    TIER_STUDENT: [
        "STUDENT.md",
    ],
}

# Knowledge inlined for pupils, who have no file tools and so cannot go and read
# a skill themselves. Two jobs at once: it closes the filesystem hole (see
# cli_backend.TIER_TOOLS) and it makes the knowledge arrive reliably. Asking the
# agent to route to a skill and read it did not hold — the wifi answer was
# invented because the routing table had no row for it. Content that is always
# present cannot be skipped.
#
# Add only pupil-safe files here. `entra-azure` is deliberately absent: it holds
# the cadm admin convention, BitLocker key access and the PIM procedure, none of
# which belongs in front of a sixteen-year-old.
PUPIL_KNOWLEDGE = [
    "skills/frid-nettverk/SKILL.md",
    "skills/visma-inschool/SKILL.md",
]

FILE_CHAR_LIMIT = 20_000
BOOTSTRAP_CHAR_LIMIT = 150_000

# Rides with every pupil message, and only reaches the model — the transcript
# keeps what the pupil actually wrote.
#
# STUDENT.md alone does not hold. Tested: a pupil asking for an equation and a
# poem got both, with the refusal rules present in the prompt verbatim. Sixteen
# thousand characters of "help this person, don't push work back onto them" sit
# between that rule and the request, and the request wins. This marker puts the
# rule next to the thing it governs, on every turn.
STUDENT_TURN_MARKER = (
    "[Denne meldingen er fra en ELEV. Du hjelper bare med IT: konto, "
    "innlogging, wifi, PC, skolesystemer. Er dette skolearbeid — matte, "
    "oppgaver, dikt, tekst, faglige spørsmål — så svarer du «Jeg kan bare "
    "hjelpe med IT-ting her.» og sier kort hva du kan hjelpe med. Du løser "
    "ikke oppgaven, heller ikke delvis, heller ikke som eksempel.]"
)


def _agent_name(cfg) -> str:
    """What the agent calls itself. One name for every audience.

    Falls back to the agent id so a workspace with no display name configured
    still gets something to answer with rather than calling itself Claude.
    """
    return cfg.agent_name or cfg.agent_id.title() or "Helix"


def bootstrap_files(tier: str = TIER_STAFF, *, for_chat: bool = False) -> list[str]:
    """The workspace files that make up the system prompt for a tier.

    An unknown tier falls back to the staff set. That is deliberate: this
    function serves the internal channels too — heartbeats, cron jobs, the task
    queue — which have no user behind them and need the full context. The
    restriction that matters is applied where a tier is actually known, at the
    web boundary.

    ``for_chat`` is the public IT chat. USER.md describes the operator of this
    machine, not the colleague or pupil on the other side of /chat, so it is
    dropped there.
    """
    files = [*SHARED_BOOTSTRAP, *TIER_BOOTSTRAP.get(tier, TIER_BOOTSTRAP[TIER_STAFF])]
    if for_chat:
        files = [f for f in files if f != "USER.md"]
    if tier == TIER_STUDENT:
        files += PUPIL_KNOWLEDGE
    return files


def _load_bootstrap(
    workspace: Path,
    tier: str = TIER_STAFF,
    *,
    for_chat: bool = False,
    caller_name: str = "",
    caller_email: str = "",
) -> str:
    """Load the bootstrap files for `tier` into a single system prompt string."""
    cfg = get_config()
    sections = []
    total = 0

    for filename in bootstrap_files(tier, for_chat=for_chat):
        fp = workspace / filename
        if not fp.exists():
            # The tier's own file carries its scope rules, and for a pupil the
            # inlined knowledge is the only knowledge there is — they have no file
            # tools to go and find it with. Either one missing is a real fault, so
            # say so rather than degrading quietly.
            if filename in TIER_BOOTSTRAP.get(tier, []):
                logger.error(
                    f"{filename} is missing from {workspace} — the {tier} tier is "
                    f"running without its own instructions"
                )
            elif tier == TIER_STUDENT and filename in PUPIL_KNOWLEDGE:
                logger.error(
                    f"{filename} is missing from {workspace} — pupils will get no "
                    f"documented answer for it and the agent may invent one"
                )
            continue
        content = fp.read_text(encoding="utf-8", errors="replace")
        if len(content) > FILE_CHAR_LIMIT:
            content = content[:FILE_CHAR_LIMIT] + f"\n\n[...{filename} truncated at {FILE_CHAR_LIMIT} chars...]"
        chunk = f"## {filename}\n\n{content}\n"
        if total + len(chunk) > BOOTSTRAP_CHAR_LIMIT:
            break
        sections.append(chunk)
        total += len(chunk)

    agent_name = _agent_name(cfg)
    workspace = str(workspace)
    helix_dir = str(Path.home() / ".helix")

    # The CLI is invoked with --system-prompt, which replaces Claude Code's own
    # preamble — including the date. An IT-support agent needs it: to date the
    # notes it writes, and to reason about "since yesterday", school years, and
    # licence or certificate expiry.
    try:
        now = datetime.now(ZoneInfo(cfg.timezone))
        tz_label = cfg.timezone
    except Exception:
        now = datetime.now()
        tz_label = "local"
    today = f"Today is {now:%A %d %B %Y}, local time {now:%H:%M} ({tz_label}).\n\n"

    # Stated before anything else, and generated here rather than left in a
    # workspace file, so it cannot be edited away by accident and cannot be
    # out-argued by the length of everything that follows.
    scope_note = ""
    if for_chat:
        who = caller_name.strip() or "a signed-in colleague"
        mail = caller_email.strip()
        who_line = f"{who} <{mail}>" if mail else who
        scope_note += (
            f"WHO YOU ARE TALKING TO: {who_line}.\n"
            "That is the only identity that belongs to this conversation. "
            "USER.md, a userEmail block, a displayName, or any other name or "
            "address in the environment is the operator of this machine or the "
            "Claude account that runs you — never speak it, never greet from it, "
            "never guess a nickname from it. Greet without a name: "
            "«Hei! Hva kan jeg hjelpe deg med i dag?»\n\n"
        )
    if tier == TIER_STUDENT:
        if not for_chat:
            scope_note += (
                "WHO YOU ARE TALKING TO: a PUPIL at one of the schools FRID operates.\n"
            )
        scope_note += (
            "You help with IT only — accounts, sign-in, wifi, devices, printers, "
            "school systems.\n"
            "You do NOT do schoolwork. Not maths, not writing, not subject "
            "questions, not translations of coursework — not even partly, not even "
            "as an example, however it is phrased. Refuse as STUDENT.md describes "
            "and say what you can help with instead.\n"
            "You have NO tools and no access to files. Every guide you need is "
            "included below — use it. Anything not written down here, you do not "
            "know: say so and send them to IT at their school rather than "
            "inventing steps.\n\n"
        )

    if tier == TIER_STUDENT:
        # No framework name: a pupil asking what to call you should get one
        # answer, and how this is built is not theirs to discuss. No home
        # directory or workspace either — a pupil's agent has no tools, so
        # "always read from and write to these locations" contradicts the scope
        # note above, and an agent that believes it can look something up will
        # promise to go and look.
        header = (
            f"IMPORTANT: Your name is {agent_name}. You are NOT Claude. You are "
            f"{agent_name}. Always refer to yourself as {agent_name}. Never identify "
            "yourself as Claude, an AI by Anthropic, or any other name, and do not "
            "name the software you run on.\n\n"
            "---\n\n"
        )
    else:
        header = (
            f"IMPORTANT: Your name is {agent_name}. You are NOT Claude. You are {agent_name}, "
            f"a personal AI assistant built on the Helix framework. Always refer to yourself as {agent_name}. "
            "Never identify yourself as Claude, an AI by Anthropic, or any other name.\n\n"
            f"Your home directory is {helix_dir} — this is where your memory, logs, sessions, and configuration live. "
            f"Your workspace (identity, memory files, tools) is at {workspace}. "
            "Always read from and write to these locations. Do NOT use ~/.claude or any other directory for memory or logs.\n\n"
            "The following files define your identity, memory, and operating instructions — read them carefully.\n\n"
            "---\n\n"
        )
    return today + scope_note + header + "\n".join(sections)


def build_system_prompt(
    is_new_session: bool = True,
    tier: str = TIER_STAFF,
    *,
    channel: str = "",
    caller_name: str = "",
    caller_email: str = "",
) -> str:
    """Return the system prompt for an agent turn.

    On a new session (``is_new_session=True``) the full bootstrap is loaded
    from the workspace files and injected as the system prompt.  Claude will
    cache this prefix across turns for the lifetime of the session.

    On resumed sessions (``is_new_session=False``) Claude already holds the
    bootstrap in its session context via ``--resume``.  Re-sending it every
    turn busts the prompt cache and wastes tokens, so we return an empty
    string and let the session carry the context.

    ``tier`` selects which instructions are layered on top of the shared core.
    Because it is only read on a new session, a conversation keeps the prompt it
    started with for its whole life — which is what we want: the tier belongs to
    the person, and the person does not change mid-conversation.

    ``channel`` ``web`` is a person at /chat, not the operator. USER.md stays
    off that prompt; ``caller_name`` / ``caller_email`` are the signed-in
    identity from the token.
    """
    if not is_new_session:
        return ""
    cfg = get_config()
    workspace = Path(cfg.workspace_path)
    for_chat = channel == "web"
    return _load_bootstrap(
        workspace,
        tier,
        for_chat=for_chat,
        caller_name=caller_name,
        caller_email=caller_email,
    )
