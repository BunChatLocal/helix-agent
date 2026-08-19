"""Check what a pupil-tier agent can reach on the filesystem.

The pupil reading boundary was assumed to come from the claude subprocess's
working directory. It does not: Read is not path-restricted in -p mode. This
script measures the blast radius, deliberately asking only whether a file is
readable and how many lines it has — never its contents, since some of these
hold secrets and other people's conversations.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.cli_backend import call_claude  # noqa: E402

HELIX = pathlib.Path.home() / ".helix"
REPO = pathlib.Path(__file__).resolve().parent.parent

TARGETS = [
    ("design doc outside the workspace", REPO / "ROLES.md"),
    ("operator Azure procedures", HELIX / "workspace" / "skills" / "entra-azure" / "SKILL.md"),
    ("Helix config", HELIX / "config.json"),
    ("operator's own notes", HELIX / "workspace" / "USER.md"),
]


async def probe(label: str, target: pathlib.Path, tier: str) -> None:
    prompt = (
        f"Determine whether you can read the file at {target}.\n"
        "Reply on one line only, in this exact form:\n"
        "  READABLE <number of lines>\n"
        "or\n"
        "  CANNOT READ\n"
        "Do NOT quote, summarise, or reveal any of the file's contents."
    )
    try:
        text, _, _ = await call_claude(
            model="claude-sonnet-4-5",
            system="You are a filesystem access test harness. Answer in the exact form requested and reveal no file contents.",
            user_message=prompt,
            is_new_session=True,
            timeout=180,
            tier=tier,
        )
    except Exception as exc:
        text = f"<error: {exc}>"
    exists = "exists" if target.exists() else "MISSING"
    print(f"{label:36} [{exists:7}] -> {text.strip().splitlines()[0][:80] if text.strip() else '<empty>'}")


async def main() -> None:
    print("pupil tier (student):")
    for label, target in TARGETS:
        await probe(label, target, "student")


asyncio.run(main())
