"""Download shelf for IT tools the operator panel can hand out.

Anyone who can open the panel may list and download. Only operators and
Users-tab admins may add or replace files. Helix never executes anything
stored here — a browser cannot start Office checkers or installers. The
person downloads the file and runs it on the PC they are checking.

Live files sit in ``~/.helix/apps`` so they survive restarts (and the
Railway volume). Optional seed apps under ``apps-seed/<id>/`` are copied
once; deleting a seeded app does not bring it back on the next boot.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path

STORE_DIR = Path.home() / ".helix" / "apps"
SEED_DIR = Path(__file__).resolve().parent.parent / "apps-seed"
CATALOG_NAME = "catalog.json"
MAX_BYTES = 200 * 1024 * 1024
NAME_MAX = 80
DESC_MAX = 500
HINT_MAX = 1000
PLATFORMS = ("windows", "mac")
ALLOWED_SUFFIXES = {
    ".zip", ".exe", ".msi", ".msix", ".dmg", ".pkg",
    ".bat", ".cmd", ".ps1", ".sh", ".command",
}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_LOCK = threading.Lock()


def _store() -> Path:
    return STORE_DIR


def _catalog_path() -> Path:
    return _store() / CATALOG_NAME


def _empty_catalog() -> dict:
    return {"apps": [], "seeded": []}


def _load() -> dict:
    path = _catalog_path()
    if not path.exists():
        return _empty_catalog()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_catalog()
    if not isinstance(data, dict):
        return _empty_catalog()
    apps = data.get("apps")
    seeded = data.get("seeded")
    if not isinstance(apps, list):
        apps = []
    if not isinstance(seeded, list):
        seeded = []
    clean = []
    for row in apps:
        if isinstance(row, dict) and ID_RE.match(str(row.get("id") or "")):
            clean.append(row)
    return {
        "apps": clean,
        "seeded": [s for s in seeded if isinstance(s, str) and ID_RE.match(s)],
    }


def _save(data: dict) -> None:
    root = _store()
    root.mkdir(parents=True, exist_ok=True)
    path = _catalog_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _clip(text: str, limit: int) -> str:
    body = (text or "").strip()
    if len(body) > limit:
        return body[:limit]
    return body


def slugify(name: str) -> str:
    """ASCII id from a display name. 'Skoleprogrammer' -> 'skoleprogrammer'."""
    out: list[str] = []
    prev_dash = False
    for ch in (name or "").strip().lower():
        if ch.isascii() and ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif ch in " -_." and out and not prev_dash:
            out.append("-")
            prev_dash = True
    slug = "".join(out).strip("-")[:63]
    if not ID_RE.match(slug):
        raise ValueError("name must contain letters or digits")
    return slug


def safe_filename(name: str) -> str:
    raw = Path(name or "").name.replace("\x00", "").strip() or "app.bin"
    if raw in {".", ".."}:
        raw = "app.bin"
    suffix = Path(raw).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            "unsupported file type — use zip, exe, msi, msix, dmg, pkg, bat, ps1 or sh"
        )
    if len(raw) > 120:
        raw = Path(raw).stem[:100] + suffix
    return raw


def parse_platforms(raw) -> list[str]:
    if isinstance(raw, str):
        parts = [p.strip().lower() for p in raw.replace(";", ",").split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip().lower() for p in raw]
    else:
        parts = []
    seen = []
    for p in parts:
        if p in PLATFORMS and p not in seen:
            seen.append(p)
    return seen or ["windows"]


def default_run_hint(filename: str, platforms: list[str]) -> str:
    suffix = Path(filename).suffix.lower()
    bits = []
    if suffix == ".zip":
        bits.append("Unpack the zip first — running a single file from inside it will not work.")
        if "windows" in platforms:
            bits.append("Windows: right-click the .bat and choose Run as administrator.")
        if "mac" in platforms:
            bits.append("Mac: right-click the .command file and choose Open.")
        return " ".join(bits)
    if suffix in {".exe", ".msi"}:
        return "Download, then double-click the file on the PC. Windows may warn that it came from the internet — choose Keep, then Run."
    if suffix == ".msix":
        return "Download, then open the package on the PC to install it."
    if suffix in {".dmg", ".pkg"}:
        return "Download, then open the file on the Mac."
    if suffix in {".bat", ".cmd", ".ps1"}:
        return "Download, then right-click and choose Run as administrator. Windows may block a file that came from the internet — Properties → Unblock."
    if suffix in {".sh", ".command"}:
        return "Download, then right-click and choose Open. macOS blocks scripts from the internet the first time."
    return "Download the file and run it on the machine you are checking. The browser cannot start it."


def check_file_meta(filename: str, size: int, head: bytes = b"") -> None:
    if size <= 0:
        raise ValueError("empty file")
    if size > MAX_BYTES:
        raise ValueError(f"file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    suffix = Path(filename).suffix.lower()
    if suffix in {".zip", ".msix"} and not head.startswith(b"PK"):
        raise ValueError("that file doesn't look like a zip")
    if suffix == ".exe" and not head.startswith(b"MZ"):
        raise ValueError("that file doesn't look like a Windows program")


def check_payload(filename: str, data: bytes) -> None:
    check_file_meta(filename, 0 if not data else len(data), data[:8] if data else b"")


def public_row(app: dict) -> dict:
    return {
        "id": app.get("id") or "",
        "name": app.get("name") or "",
        "description": app.get("description") or "",
        "platforms": list(app.get("platforms") or []),
        "filename": app.get("filename") or "",
        "size": int(app.get("size") or 0),
        "uploaded_at": app.get("uploaded_at") or 0,
        "uploaded_by": app.get("uploaded_by") or "",
        "run_hint": app.get("run_hint") or "",
    }


def file_path(app_id: str, filename: str | None = None) -> Path | None:
    if not ID_RE.match(app_id or ""):
        return None
    folder = _store() / app_id
    if filename:
        name = Path(filename).name
        path = (folder / name).resolve()
        try:
            path.relative_to(folder.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None
    return folder if folder.is_dir() else None


def _zip_payload(payload_dir: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(payload_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(payload_dir).as_posix())


def _install_seed_folder(seed_dir: Path) -> dict | None:
    man_path = seed_dir / "manifest.json"
    if not man_path.is_file():
        return None
    try:
        manifest = json.loads(man_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    app_id = str(manifest.get("id") or seed_dir.name).strip()
    if not ID_RE.match(app_id):
        return None
    name = _clip(str(manifest.get("name") or app_id), NAME_MAX)
    filename = str(manifest.get("filename") or "").strip()
    if not filename:
        return None
    try:
        filename = safe_filename(filename)
    except ValueError:
        return None
    dest_dir = _store() / app_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    src_file = seed_dir / filename
    payload = seed_dir / "payload"
    try:
        if src_file.is_file():
            shutil.copy2(src_file, dest)
        elif payload.is_dir():
            _zip_payload(payload, dest)
        else:
            return None
    except OSError:
        return None
    if not dest.is_file():
        return None
    platforms = parse_platforms(manifest.get("platforms"))
    hint = _clip(str(manifest.get("run_hint") or ""), HINT_MAX) or default_run_hint(filename, platforms)
    return {
        "id": app_id,
        "name": name,
        "description": _clip(str(manifest.get("description") or ""), DESC_MAX),
        "platforms": platforms,
        "filename": filename,
        "size": dest.stat().st_size,
        "uploaded_at": time.time(),
        "uploaded_by": "seed",
        "run_hint": hint,
    }


def _adopt_printer_file() -> None:
    """Register VikenPrint.msi if it is already on the shelf without a catalog row."""
    folder = _store() / "printer"
    if not folder.is_dir():
        return
    msi = None
    for path in sorted(folder.iterdir()):
        if path.is_file() and path.suffix.lower() == ".msi":
            msi = path
            break
    if msi is None:
        return
    with _LOCK:
        data = _load()
        if any(a.get("id") == "printer" for a in data["apps"]):
            return
        data["apps"].append({
            "id": "printer",
            "name": "printer",
            "description": "Viken Print / YSoft SafeQ FlexiSpooler for the school printers.",
            "platforms": ["windows"],
            "filename": msi.name,
            "size": msi.stat().st_size,
            "uploaded_at": msi.stat().st_mtime,
            "uploaded_by": "volume",
            "run_hint": "I Zorken: trykk Skaff printer. Godkjenn i Windows.",
        })
        _save(data)


def ensure_seeds() -> None:
    """Copy each seed app once. A later delete is remembered in ``seeded``."""
    _adopt_printer_file()
    seed_root = SEED_DIR
    if not seed_root.is_dir():
        return
    with _LOCK:
        data = _load()
        seeded = set(data["seeded"])
        by_id = {a["id"]: a for a in data["apps"]}
        changed = False
        for folder in sorted(p for p in seed_root.iterdir() if p.is_dir()):
            app_id = folder.name
            if not ID_RE.match(app_id) or app_id in seeded:
                continue
            seeded.add(app_id)
            changed = True
            if app_id in by_id:
                continue
            row = _install_seed_folder(folder)
            if row:
                data["apps"].append(row)
                by_id[row["id"]] = row
        if changed:
            data["seeded"] = sorted(seeded)
            _save(data)


def list_apps() -> list[dict]:
    ensure_seeds()
    with _LOCK:
        rows = [_ for _ in _load()["apps"]]
    rows.sort(key=lambda a: (str(a.get("name") or "").lower(), a.get("id") or ""))
    return [public_row(a) for a in rows]


def get_app(app_id: str) -> dict | None:
    if not ID_RE.match(app_id or ""):
        return None
    ensure_seeds()
    with _LOCK:
        for row in _load()["apps"]:
            if row.get("id") == app_id:
                return dict(row)
    return None


def put_app(
    *,
    name: str,
    data: bytes,
    filename: str,
    description: str = "",
    platforms=None,
    run_hint: str = "",
    app_id: str = "",
    uploaded_by: str = "",
) -> dict:
    name = _clip(name, NAME_MAX)
    if not name:
        raise ValueError("name is required")
    filename = safe_filename(filename)
    check_payload(filename, data)
    wanted_id = (app_id or "").strip() or slugify(name)
    if not ID_RE.match(wanted_id):
        raise ValueError("invalid app id")
    platforms = parse_platforms(platforms)
    hint = _clip(run_hint, HINT_MAX) or default_run_hint(filename, platforms)
    now = time.time()
    with _LOCK:
        catalog = _load()
        existing = next((a for a in catalog["apps"] if a["id"] == wanted_id), None)
        folder = _store() / wanted_id
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / filename
        if existing:
            old = folder / Path(existing.get("filename") or "").name
            if old.is_file() and old.resolve() != dest.resolve():
                try:
                    old.unlink()
                except OSError:
                    pass
        dest.write_bytes(data)
        row = {
            "id": wanted_id,
            "name": name,
            "description": _clip(description, DESC_MAX),
            "platforms": platforms,
            "filename": filename,
            "size": len(data),
            "uploaded_at": now,
            "uploaded_by": _clip(uploaded_by, 120),
            "run_hint": hint,
        }
        if existing:
            catalog["apps"] = [row if a["id"] == wanted_id else a for a in catalog["apps"]]
        else:
            catalog["apps"].append(row)
        _save(catalog)
    return public_row(row)


def put_app_from_path(
    *,
    name: str,
    src: str | Path,
    filename: str = "",
    description: str = "",
    platforms=None,
    run_hint: str = "",
    app_id: str = "",
    uploaded_by: str = "",
) -> dict:
    """Copy a file from disk into the shelf without loading it into memory."""
    src_path = Path(src)
    if not src_path.is_file():
        raise ValueError("file not found")
    name = _clip(name, NAME_MAX)
    if not name:
        raise ValueError("name is required")
    filename = safe_filename(filename or src_path.name)
    size = src_path.stat().st_size
    with src_path.open("rb") as fh:
        head = fh.read(8)
    check_file_meta(filename, size, head)
    wanted_id = (app_id or "").strip() or slugify(name)
    if not ID_RE.match(wanted_id):
        raise ValueError("invalid app id")
    platforms = parse_platforms(platforms)
    hint = _clip(run_hint, HINT_MAX) or default_run_hint(filename, platforms)
    now = time.time()
    with _LOCK:
        catalog = _load()
        existing = next((a for a in catalog["apps"] if a["id"] == wanted_id), None)
        folder = _store() / wanted_id
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / filename
        if existing:
            old = folder / Path(existing.get("filename") or "").name
            if old.is_file() and old.resolve() != dest.resolve() and old.resolve() != src_path.resolve():
                try:
                    old.unlink()
                except OSError:
                    pass
        if dest.resolve() != src_path.resolve():
            shutil.copy2(src_path, dest)
        row = {
            "id": wanted_id,
            "name": name,
            "description": _clip(description, DESC_MAX),
            "platforms": platforms,
            "filename": filename,
            "size": dest.stat().st_size,
            "uploaded_at": now,
            "uploaded_by": _clip(uploaded_by, 120),
            "run_hint": hint,
        }
        if existing:
            catalog["apps"] = [row if a["id"] == wanted_id else a for a in catalog["apps"]]
        else:
            catalog["apps"].append(row)
        _save(catalog)
    return public_row(row)


def delete_app(app_id: str) -> bool:
    if not ID_RE.match(app_id or ""):
        return False
    with _LOCK:
        catalog = _load()
        if not any(a.get("id") == app_id for a in catalog["apps"]):
            return False
        catalog["apps"] = [a for a in catalog["apps"] if a.get("id") != app_id]
        if app_id not in catalog["seeded"]:
            catalog["seeded"].append(app_id)
        _save(catalog)
    folder = _store() / app_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
    return True
