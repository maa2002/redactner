"""Store model files larger than a Git host's per-file limit as parts, and join them on first load.

``split_large_files`` cuts every file above ``max_bytes`` into ``<name>.partNN`` pieces and records the
pieces and a SHA-256 checksum in ``parts.json``. ``join_split_files`` restores the original files, checking
the checksum, so the folder is a standard Hugging Face checkpoint again. The joined files are listed in a
local ``.gitignore`` so they are never committed themselves.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PARTS_FILE = "parts.json"
CHUNK = 8 * 2**20


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(CHUNK):
            digest.update(block)
    return digest.hexdigest()


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
                parts.append(part.name)
        manifest[path.name] = {"parts": parts, "sha256": _sha256(path), "bytes": path.stat().st_size}
        split.append(path.name)
    if manifest:
        (folder / PARTS_FILE).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        (folder / ".gitignore").write_text("".join(f"/{name}\n" for name in manifest), encoding="utf-8")
        for name in manifest:
            (folder / name).unlink()
    return split


def join_split_files(folder: str | Path) -> list[str]:
    """Rebuild files listed in ``parts.json`` that are missing or incomplete; returns the names rebuilt."""
    folder = Path(folder)
    manifest_path = folder / PARTS_FILE
    if not manifest_path.exists():
        return []
    rebuilt = []
    for name, entry in json.loads(manifest_path.read_text(encoding="utf-8")).items():
        target = folder / name
        if target.exists() and target.stat().st_size == entry["bytes"]:
            continue
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "wb") as out:
            for part in entry["parts"]:
                out.write((folder / part).read_bytes())
        if _sha256(tmp) != entry["sha256"]:
            tmp.unlink()
            raise OSError(f"{name}: the joined parts do not match the recorded checksum; download the model again.")
        tmp.replace(target)
        rebuilt.append(name)
    return rebuilt
