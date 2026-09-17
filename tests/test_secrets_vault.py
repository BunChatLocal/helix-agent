"""Real Fernet vault: volume-stable key, unreadable ciphertext is not wiped on read."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _reload_real_secrets():
    # Do not drop cryptography/argon2 from sys.modules — that breaks the
    # OpenSSL rust backend for the rest of the process.
    sys.modules.pop("security.secrets", None)
    import security.secrets as secrets
    return secrets


@pytest.fixture
def helix_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda *a, **k: tmp_path)
    secrets = _reload_real_secrets()
    secrets.reset_runtime_state()
    yield tmp_path, secrets
    secrets.reset_runtime_state()


class TestVolumeStableVault:
    def test_secret_survives_a_new_container_machine_id(self, helix_home):
        tmp, secrets = helix_home
        secrets.set_secret("WEB_JWT_SECRET", "keep-me")
        secrets.reset_runtime_state()
        secrets._platform_machine_entropy = lambda: b"brand-new-railway-container"
        assert secrets.get_secret("WEB_JWT_SECRET") == "keep-me"
        assert (tmp / ".helix" / ".machine-entropy").is_file()

    def test_read_does_not_overwrite_an_unreadable_vault(self, helix_home):
        tmp, secrets = helix_home
        secrets.set_secret("DEV_LOGIN_PASSWORDS", '{"a@b.no":"hash"}')
        enc = (tmp / ".helix" / "secrets.enc").read_bytes()
        secrets.reset_runtime_state()
        (tmp / ".helix" / ".machine-entropy").write_bytes(b"wrong-key-material")
        assert secrets.get_secret("DEV_LOGIN_PASSWORDS") is None
        assert (tmp / ".helix" / "secrets.enc").read_bytes() == enc

    def test_write_moves_unreadable_ciphertext_aside(self, helix_home):
        tmp, secrets = helix_home
        secrets.set_secret("OLD", "gone")
        secrets.reset_runtime_state()
        (tmp / ".helix" / ".machine-entropy").write_bytes(b"wrong-key-material")
        secrets.reset_runtime_state()
        secrets.set_secret("NEW", "fresh")
        assert secrets.get_secret("NEW") == "fresh"
        assert secrets.get_secret("OLD") is None
        backups = list((tmp / ".helix").glob("secrets.enc.unreadable*"))
        assert backups


class TestPrepareRuntimeImport:
    def test_imports_seed_without_touching_user_overrides(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(Path, "home", lambda *a, **k: tmp_path)
        secrets = _reload_real_secrets()
        secrets.reset_runtime_state()

        helix = tmp_path / ".helix"
        helix.mkdir()
        (helix / "config.json").write_text(
            json.dumps({
                "tiers": {
                    "user_overrides": [
                        {"email": "kept@frid-iks.no", "access": "admin", "note": "prod"},
                    ],
                },
            }),
            encoding="utf-8",
        )
        secrets.set_secret("OLD", "x")
        secrets.reset_runtime_state()
        (helix / ".machine-entropy").write_bytes(b"wrong-key-material")
        secrets.reset_runtime_state()

        root = tmp_path / "app"
        seed = root / "deploy" / "helix-home"
        seed.mkdir(parents=True)
        (seed / "secrets.json").write_text(
            json.dumps({"WEB_JWT_SECRET": "from-seed", "DEV_LOGIN_PASSWORDS": "{}"}),
            encoding="utf-8",
        )

        spec = importlib.util.spec_from_file_location(
            "prepare_runtime_under_test",
            Path(__file__).resolve().parent.parent / "deploy" / "prepare_runtime.py",
        )
        prepare = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(prepare)
        monkeypatch.setattr(prepare, "HOME", helix)
        monkeypatch.setattr(prepare, "ROOT", root)
        prepare.main()

        cfg = json.loads((helix / "config.json").read_text(encoding="utf-8"))
        assert cfg["tiers"]["user_overrides"][0]["email"] == "kept@frid-iks.no"
        secrets.reset_runtime_state()
        assert secrets.get_secret("WEB_JWT_SECRET") == "from-seed"
        out = capsys.readouterr().out
        assert "imported 2 secret keys" in out

    def test_refreshes_live_staff_md_wifi_instructions(self, tmp_path, monkeypatch, capsys):
        helix = tmp_path / ".helix"
        helix.mkdir()
        (helix / "config.json").write_text("{}", encoding="utf-8")
        live = helix / "workspace"
        live.mkdir()
        (live / "STAFF.md").write_text(
            "A colleague at FRID.\n\n"
            "If they ask about **wifi**, send them to onboard.frid-iks.no first.\n",
            encoding="utf-8",
        )
        root = tmp_path / "app"
        tmpl = root / "workspace-template"
        tmpl.mkdir(parents=True)
        (tmpl / "STAFF.md").write_text(
            "If they ask about **wifi / internet / skolenett on this Windows PC**, "
            "tell them to click **FRID-nett**. Zork Helper joins **FRID-iot** itself.\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location(
            "prepare_runtime_staff_wifi",
            Path(__file__).resolve().parent.parent / "deploy" / "prepare_runtime.py",
        )
        prepare = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(prepare)
        monkeypatch.setattr(prepare, "HOME", helix)
        monkeypatch.setattr(prepare, "ROOT", root)
        prepare._refresh_staff_wifi_instructions()
        text = (live / "STAFF.md").read_text(encoding="utf-8")
        assert "Zork Helper joins **FRID-iot**" in text
        assert "onboard.frid-iks.no first" not in text
        out = capsys.readouterr().out
        assert "STAFF.md wifi helper instructions updated" in out

    def test_refreshes_live_wifi_skill_so_pupils_get_frid_nett(self, tmp_path, monkeypatch, capsys):
        helix = tmp_path / ".helix"
        live = helix / "workspace" / "skills" / "frid-nettverk"
        live.mkdir(parents=True)
        (helix / "config.json").write_text("{}", encoding="utf-8")
        (live / "SKILL.md").write_text(
            "Tell them to click **FRID-nett**.\n"
            "Pupils do not have FRID-nett — they use **Last ned sertifikat**.\n",
            encoding="utf-8",
        )
        (helix / "workspace" / "STUDENT.md").write_text(
            "Once they are at school, press **Last ned sertifikat**.\n",
            encoding="utf-8",
        )
        root = tmp_path / "app"
        tmpl = root / "workspace-template"
        skill_tmpl = tmpl / "skills" / "frid-nettverk"
        skill_tmpl.mkdir(parents=True)
        (skill_tmpl / "SKILL.md").write_text(
            "Tell them to press **FRID-nett** under the chat.\n",
            encoding="utf-8",
        )
        (tmpl / "STUDENT.md").write_text(
            "If they are on a Windows PC, tell them to press **FRID-nett**.\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location(
            "prepare_runtime_pupil_wifi",
            Path(__file__).resolve().parent.parent / "deploy" / "prepare_runtime.py",
        )
        prepare = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(prepare)
        monkeypatch.setattr(prepare, "HOME", helix)
        monkeypatch.setattr(prepare, "ROOT", root)
        prepare._refresh_pupil_wifi_helper()
        skill = (live / "SKILL.md").read_text(encoding="utf-8")
        assert "Pupils do not have FRID-nett" not in skill
        assert "press **FRID-nett**" in skill
        student = (helix / "workspace" / "STUDENT.md").read_text(encoding="utf-8")
        assert "press **FRID-nett**" in student
        out = capsys.readouterr().out
        assert "pupils get FRID-nett" in out
        assert "STUDENT.md wifi helper instructions updated" in out

    def test_copies_printer_msi_onto_the_volume_once(self, tmp_path, monkeypatch, capsys):
        helix = tmp_path / ".helix"
        helix.mkdir()
        (helix / "config.json").write_text("{}", encoding="utf-8")
        root = tmp_path / "app"
        src_dir = root / "apps-seed" / "printer"
        src_dir.mkdir(parents=True)
        src = src_dir / "VikenPrint.msi"
        payload = b"\xd0\xcf\x11\xe0" + b"printer" * 20
        src.write_bytes(payload)
        spec = importlib.util.spec_from_file_location(
            "prepare_runtime_printer_msi",
            Path(__file__).resolve().parent.parent / "deploy" / "prepare_runtime.py",
        )
        prepare = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(prepare)
        monkeypatch.setattr(prepare, "HOME", helix)
        monkeypatch.setattr(prepare, "ROOT", root)
        prepare._seed_printer_msi()
        dest = helix / "apps" / "printer" / "VikenPrint.msi"
        assert dest.is_file()
        assert dest.read_bytes() == payload
        out = capsys.readouterr().out
        assert "copied printer MSI" in out
        prepare._seed_printer_msi()
        out = capsys.readouterr().out
        assert "already on volume" in out
        assert dest.read_bytes() == payload

    def test_renames_live_display_name_to_zorken(self, tmp_path, monkeypatch, capsys):
        helix = tmp_path / ".helix"
        ws = helix / "workspace"
        ws.mkdir(parents=True)
        (helix / "config.json").write_text(
            json.dumps({"agent_name": "Zork"}), encoding="utf-8"
        )
        (ws / "IDENTITY.md").write_text("- **Name:** Zork\n", encoding="utf-8")
        (ws / "AGENTS.md").write_text(
            '"Hei, jeg er Zork, din IT-hjelper" is exactly wrong.\n',
            encoding="utf-8",
        )
        apps = helix / "apps"
        apps.mkdir()
        (apps / "catalog.json").write_text(
            '{"run_hint": "I Zork: trykk FRID-nett. Zork Helper kobler til."}\n',
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location(
            "prepare_runtime_zorken",
            Path(__file__).resolve().parent.parent / "deploy" / "prepare_runtime.py",
        )
        prepare = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(prepare)
        monkeypatch.setattr(prepare, "HOME", helix)
        monkeypatch.setattr(prepare, "ROOT", tmp_path / "app")
        prepare.main()
        cfg = json.loads((helix / "config.json").read_text(encoding="utf-8"))
        assert cfg["agent_name"] == "Zorken"
        identity = (ws / "IDENTITY.md").read_text(encoding="utf-8")
        assert "**Name:** Zorken" in identity
        agents = (ws / "AGENTS.md").read_text(encoding="utf-8")
        assert "Zorken, din IT-hjelper" in agents
        catalog = (apps / "catalog.json").read_text(encoding="utf-8")
        assert "I Zorken:" in catalog
        assert "Zork Helper kobler" in catalog
        out = capsys.readouterr().out
        assert "renamed Zork to Zorken" in out
