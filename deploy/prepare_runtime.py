"""Rewrite Windows Helix home for Linux and re-encrypt secrets for this machine."""

from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

HOME = Path.home() / ".helix"

# User-facing product name. Do not match "Zork Helper" / "ZorkHelper".
_DISPLAY_NAME_RE = re.compile(r"\bZork\b(?! Helper)(?! helper)")


def _rewrite_config() -> None:
    path = HOME / "config.json"
    if not path.is_file():
        print("prepare_runtime: no config.json", file=sys.stderr)
        return
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["workspace_path"] = str(HOME / "workspace")
    if cfg.get("agent_name") == "Zork":
        cfg["agent_name"] = "Zorken"
    web = cfg.setdefault("web", {})
    web["host"] = "0.0.0.0"
    web["dev_login"] = True
    entra = cfg.setdefault("entra", {})
    entra["enabled"] = False
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (HOME / "workspace").mkdir(parents=True, exist_ok=True)
    print("prepare_runtime: workspace_path and web.host rewritten")


def _seed_secrets_path() -> Path | None:
    candidates = [
        HOME / "secrets.json",
        ROOT / "deploy" / "secrets.import.json",
        ROOT / "deploy" / "helix-home" / "secrets.json",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _import_secrets() -> None:
    from security.secrets import rotate_unreadable_vault, set_secret, vault_is_readable

    if vault_is_readable():
        print("prepare_runtime: vault readable, not importing secrets.json")
        leftover = HOME / "secrets.json"
        if leftover.is_file():
            try:
                leftover.unlink()
            except OSError:
                pass
        return

    seed = _seed_secrets_path()
    if seed is None:
        print("prepare_runtime: vault unreadable or empty, and no secrets.json to import")
        return

    if (HOME / "secrets.enc").is_file() and not vault_is_readable():
        rotate_unreadable_vault()

    data = json.loads(seed.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        print("prepare_runtime: secrets.json is not an object", file=sys.stderr)
        return
    count = 0
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str) and value:
            set_secret(key, value)
            count += 1
    print(f"prepare_runtime: imported {count} secret keys from {seed}")
    if seed == HOME / "secrets.json":
        try:
            seed.unlink()
        except OSError:
            pass


def _refresh_staff_wifi_instructions() -> None:
    """Bring Railway's live STAFF.md in line with the repo wifi helper flow.

    The volume keeps an old STAFF.md across deploys. Setup never overwrites
    files that already exist, so staff chat would keep telling people to join
    FRID-iot by hand. Copy the template when the live file still has that
    old paragraph.
    """
    live = HOME / "workspace" / "STAFF.md"
    tmpl = ROOT / "workspace-template" / "STAFF.md"
    if not tmpl.is_file():
        return
    live.parent.mkdir(parents=True, exist_ok=True)
    if not live.is_file():
        shutil.copy2(tmpl, live)
        print("prepare_runtime: STAFF.md copied from template")
        return
    try:
        text = live.read_text(encoding="utf-8")
    except OSError:
        return
    if "Zork Helper joins **FRID-iot**" in text and "that button is hidden" not in text and "Last ned sertifikat" not in text and "**Skaff printer**" in text and "macOS updates" in text and "is not shown" in text:
        return
    shutil.copy2(tmpl, live)
    print("prepare_runtime: STAFF.md wifi helper instructions updated from template")


def _refresh_pupil_wifi_helper() -> None:
    """Live volume may still say pupils have no FRID-nett button."""
    skill_live = HOME / "workspace" / "skills" / "frid-nettverk" / "SKILL.md"
    skill_tmpl = ROOT / "workspace-template" / "skills" / "frid-nettverk" / "SKILL.md"
    if skill_tmpl.is_file():
        skill_live.parent.mkdir(parents=True, exist_ok=True)
        if not skill_live.is_file():
            shutil.copy2(skill_tmpl, skill_live)
            print("prepare_runtime: wifi skill copied from template")
        else:
            try:
                text = skill_live.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if "Pupils do not have FRID-nett" in text or "under the composer" in text or "if they can see **FRID-nett**" not in text or "a pupil has none" in text:
                shutil.copy2(skill_tmpl, skill_live)
                print("prepare_runtime: wifi skill updated so pupils get FRID-nett")
    student_live = HOME / "workspace" / "STUDENT.md"
    student_tmpl = ROOT / "workspace-template" / "STUDENT.md"
    if not student_tmpl.is_file():
        return
    if not student_live.is_file():
        shutil.copy2(student_tmpl, student_live)
        print("prepare_runtime: STUDENT.md copied from template")
        return
    try:
        text = student_live.read_text(encoding="utf-8")
    except OSError:
        return
    if "press **FRID-nett**" in text and "Last ned sertifikat" not in text and "**Skaff printer**" in text and "ZorkHelper.command" in text and "they see **FRID-nett**" in text and "Name the button and stop" in text and "Their PC is their own" in text:
        return
    shutil.copy2(student_tmpl, student_live)
    print("prepare_runtime: STUDENT.md wifi helper instructions updated from template")


def _rename_display_to_zorken() -> None:
    """The volume keeps IDENTITY/AGENTS/catalog across deploys. Rename the
    product people see without touching Zork Helper."""
    targets = [
        HOME / "workspace" / "IDENTITY.md",
        HOME / "workspace" / "AGENTS.md",
        HOME / "workspace" / "skills" / "frid-nettverk" / "SKILL.md",
        HOME / "apps" / "catalog.json",
    ]
    for path in targets:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = _DISPLAY_NAME_RE.sub("Zorken", text)
        if updated == text:
            continue
        path.write_text(updated, encoding="utf-8")
        print(f"prepare_runtime: renamed Zork to Zorken in {path.name}")


def _seed_printer_msi() -> None:
    """Copy VikenPrint.msi from the image onto the volume once."""
    src = None
    for rel in (
        ROOT / "apps-seed" / "printer" / "VikenPrint.msi",
        ROOT / "apps-payload" / "printer" / "VikenPrint.msi",
    ):
        if rel.is_file():
            src = rel
            break
    if src is None:
        return
    dest_dir = HOME / "apps" / "printer"
    dest = dest_dir / "VikenPrint.msi"
    try:
        if dest.is_file() and dest.stat().st_size == src.stat().st_size:
            print("prepare_runtime: printer MSI already on volume")
            return
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        print(f"prepare_runtime: copied printer MSI ({dest.stat().st_size} bytes)")
    except OSError as exc:
        print(f"prepare_runtime: could not copy printer MSI: {exc}", file=sys.stderr)


def _refresh_windows_endpoint_printer() -> None:
    live = HOME / "workspace" / "skills" / "windows-endpoint" / "SKILL.md"
    tmpl = ROOT / "workspace-template" / "skills" / "windows-endpoint" / "SKILL.md"
    if not tmpl.is_file():
        return
    live.parent.mkdir(parents=True, exist_ok=True)
    if not live.is_file():
        shutil.copy2(tmpl, live)
        print("prepare_runtime: windows-endpoint skill copied from template")
        return
    try:
        text = live.read_text(encoding="utf-8")
    except OSError:
        return
    if "**Skaff printer**" in text:
        return
    shutil.copy2(tmpl, live)
    print("prepare_runtime: windows-endpoint printer button updated from template")


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    _rewrite_config()
    _import_secrets()
    _seed_printer_msi()
    _refresh_staff_wifi_instructions()
    _refresh_pupil_wifi_helper()
    _refresh_windows_endpoint_printer()
    _rename_display_to_zorken()


if __name__ == "__main__":
    main()
