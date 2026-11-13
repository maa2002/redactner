"""Store model files larger than a Git host's per-file limit as parts, and join them on first load.

``split_large_files`` cuts every file above ``max_bytes`` into ``<name>.partNN`` pieces and records the
pieces and a SHA-256 checksum in ``parts.json``. ``join_split_files`` restores the original files, checking
the checksum, so the folder is a standard Hugging Face checkpoint again. The joined files are listed in a
local ``.gitignore`` so they are never committed themselves.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

PARTS_FILE = "parts.json"
CHUNK = 8 * 2**20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


def _check_name(name: object, what: str, prefix: str | None = None) -> str:
    """Accept only a bare file name, so a crafted manifest cannot reach outside the model folder."""
    if (not isinstance(name, str) or not name or name in (".", "..") or "/" in name or "\\" in name
            or ":" in name or "\0" in name or Path(name).name != name):
        raise OSError(f"{PARTS_FILE}: unsafe {what} name {name!r}; the model folder may be tampered with.")
    if prefix is not None and not name.startswith(prefix):
        raise OSError(f"{PARTS_FILE}: part name {name!r} must start with {prefix!r}.")
    return name


def split_large_files(folder: str | Path, max_bytes: int = 45 * 10**6) -> list[str]:
    """Split files larger than ``max_bytes`` into parts; returns the names of the split files."""
    folder = Path(folder)
    manifest, split = {}, []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.stat().st_size <= max_bytes or path.name in (PARTS_FILE, ".gitignore"):
            continue
        parts = []
        with open(path, "rb") as fh:
            while data := fh.read(max_bytes):
                part = folder / f"{path.name}.part{len(parts):02d}"
                part.write_bytes(data)
                parts.append(_check_name(part.name, "part", f"{path.name}.part"))
        manifest[_check_name(path.name, "file")] = {"parts": parts, "sha256": _sha256(path),
                                                    "bytes": path.stat().st_size}
        split.append(path.name)
    if manifest:
        (folder / PARTS_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (folder / ".gitignore").write_text("".join(f"/{name}\n" for name in manifest), encoding="utf-8")
        for name in manifest:
            (folder / name).unlink()
    return split


def _read_manifest(manifest_path: Path) -> dict[str, dict]:
    """Load and validate ``parts.json``; any problem becomes one clear ``OSError``."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OSError(f"{PARTS_FILE} is not valid JSON ({exc}); download the model again.") from exc
    if not isinstance(manifest, dict):
        raise OSError(f"{PARTS_FILE} has the wrong shape (expected an object of files); download the model again.")
    for name, entry in manifest.items():
        _check_name(name, "file")
        ok = (isinstance(entry, dict) and isinstance(entry.get("parts"), list) and entry["parts"]
              and isinstance(entry.get("sha256"), str) and isinstance(entry.get("bytes"), int)
              and not isinstance(entry["bytes"], bool))
        if not ok:
            raise OSError(f"{PARTS_FILE}: entry for {name!r} needs 'parts', 'sha256' and 'bytes'; "
                          "download the model again.")
        for part in entry["parts"]:
            _check_name(part, "part", f"{name}.part")
    return manifest


def join_split_files(folder: str | Path) -> list[str]:
    """Rebuild files listed in ``parts.json`` that are missing or incomplete; returns the names rebuilt."""
    folder = Path(folder)
    manifest_path = folder / PARTS_FILE
    if not manifest_path.exists():
        return []
    rebuilt = []
    for name, entry in _read_manifest(manifest_path).items():
        target = folder / name
        if target.exists() and target.stat().st_size == entry["bytes"]:
            continue
        tmp = folder / f"{name}.{uuid.uuid4().hex}.tmp"  # unique, so a stale or planted file is never reused
        try:
            with open(tmp, "wb") as out:
                for part in entry["parts"]:
                    with open(folder / part, "rb") as fh:
                        while block := fh.read(CHUNK):
                            out.write(block)
            if _sha256(tmp) != entry["sha256"]:
                raise OSError(f"{name}: the joined parts do not match the recorded checksum; "
                              "download the model again.")
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        rebuilt.append(name)
    return rebuilt

