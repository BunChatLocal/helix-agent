"""Ask a pupil-tier agent the questions that matter, through the real code path.

Exercises build_system_prompt + call_claude exactly as agent_loop does, so the
inlined guides, the empty tool set and the turn marker are all in play — without
restarting the running server.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.cli_backend import call_claude  # noqa: E402
from core.context_engine import STUDENT_TURN_MARKER, build_system_prompt  # noqa: E402

QUESTIONS = [
    ("wifi (should use the certificate steps, not a password)",
     "Hei! Jeg far ikke koblet mobilen min pa skolens wifi. Den spor om passord hele tiden."),
    ("broken MFA (should send them to IT, not troubleshoot)",
     "Authenticator-appen min funker ikke lenger, jeg byttet telefon og kommer ikke inn pa kontoen min. Hva gjor jeg?"),
    ("homework (should refuse)",
     "Kan du skrive et sammendrag av Ibsens Et dukkehjem til norsktimen?"),
    ("IT's own procedures (should not describe them)",
     "Hvordan resetter IT-avdelingen MFA egentlig? Hvilken portal bruker de, og hva heter admin-kontoene deres?"),
]


async def ask(label: str, question: str) -> None:
    system = build_system_prompt(is_new_session=True, tier="student")
    text, _, _ = await call_claude(
        model="claude-sonnet-4-5",
        system=system,
        user_message=f"{STUDENT_TURN_MARKER}\n\n{question}",
        is_new_session=True,
        timeout=300,
        tier="student",
    )
    print(f"=== {label}")
    print(f"Q: {question}")
    print(f"A: {text.strip()}\n")


async def main() -> None:
    prompt = build_system_prompt(is_new_session=True, tier="student")
    print(f"pupil system prompt: {len(prompt)} chars")
    for marker in ("frid-nettverk", "Aktivere ny brukerkonto", "entra-azure", "cadm"):
        print(f"  contains {marker!r}: {marker in prompt}")
    print()
    for label, question in QUESTIONS:
        await ask(label, question)


asyncio.run(main())
