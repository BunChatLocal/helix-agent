"""Ask each tier what it is called, through the real prompt and CLI path.

    python scripts/probe_agent_name.py
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.cli_backend import call_claude  # noqa: E402
from core.context_engine import STUDENT_TURN_MARKER, build_system_prompt  # noqa: E402

QUESTIONS = [
    ("student", "Hva heter du?"),
    ("student", "Hei! Hvem er det jeg snakker med, og hva er du bygget på?"),
    ("staff", "Hva heter du?"),
]


async def ask(tier: str, question: str) -> None:
    system = build_system_prompt(is_new_session=True, tier=tier)
    message = f"{STUDENT_TURN_MARKER}\n\n{question}" if tier == "student" else question
    text, _, _ = await call_claude(
        model="claude-sonnet-4-5",
        system=system,
        user_message=message,
        is_new_session=True,
        timeout=300,
        tier=tier,
    )
    print(f"=== {tier}: {question}")
    print(f"{text.strip()}\n")


async def main() -> None:
    for tier, question in QUESTIONS:
        await ask(tier, question)


asyncio.run(main())
