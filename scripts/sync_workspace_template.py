"""Copy prompt/knowledge files from the live workspace into workspace-template.

The template is what a fresh install starts from, so edits made against the live
workspace have to be mirrored or a new install ships the old behaviour.
"""
import pathlib
import shutil
import sys

WORKSPACE = pathlib.Path.home() / ".helix" / "workspace"
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "workspace-template"

FILES = [
    "AGENTS.md",
    "SOUL.md",
    "OPERATIONS.md",
    "CLAUDE.md",
    "STUDENT.md",
    "STAFF.md",
    "MODERATOR.md",
    "ADMIN.md",
    "skills/frid-nettverk/SKILL.md",
]

for rel in sys.argv[1:] or FILES:
    src = WORKSPACE / rel
    dst = TEMPLATE / rel
    if not src.exists():
        print(f"SKIP {rel} — not in workspace")
        continue
    dst.parent.mkdir(parents=True, exist_ok=True)
    same = dst.exists() and dst.read_bytes() == src.read_bytes()
    shutil.copyfile(src, dst)
    print(f"{'unchanged' if same else 'updated  '} {rel}")
