"""Smoke-test both tiers through call_claude after the prompt-file change.

Staff moved to --system-prompt-file alongside pupils, and pupils moved to
stream-json input, so both paths need exercising: a staff turn that still uses
its file tools, and a pupil turn carrying an image inline.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.cli_backend import call_claude  # noqa: E402
from core.context_engine import build_system_prompt  # noqa: E402

IMAGE = pathlib.Path.home() / ".helix" / "workspace" / "assets" / "del-2-reset-mfa-20260814-1.png"


async def main() -> None:
    staff_system = build_system_prompt(is_new_session=True, tier="staff")
    print(f"staff prompt: {len(staff_system)} chars")
    text, sid, usage = await call_claude(
        model="claude-sonnet-4-5",
        system=staff_system,
        user_message="In one short line: what is the name of the skill file covering FRID wifi onboarding? Read skills/ to check.",
        is_new_session=True,
        timeout=300,
        tier="staff",
    )
    print(f"staff reply : {text.strip()[:220]}")
    print(f"staff tokens: in={usage['input_tokens']} out={usage['output_tokens']}\n")

    pupil_system = build_system_prompt(is_new_session=True, tier="student")
    text, sid, usage = await call_claude(
        model="claude-sonnet-4-5",
        system=pupil_system,
        user_message="Hva viser dette bildet? Svar kort.",
        is_new_session=True,
        timeout=300,
        tier="student",
        images=[IMAGE],
    )
    print(f"pupil+image reply : {text.strip()[:220]}")
    print(f"pupil tokens      : in={usage['input_tokens']} out={usage['output_tokens']}")


asyncio.run(main())
