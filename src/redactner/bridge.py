"""What the page in the desktop window may ask the application to do. No network is involved.

The page gets exactly two plain functions:

* ``request(method, path, payload)`` carries the page's calls (open a document, analyse it, redact it)
  to :class:`redactner.service.RedactionService`. It uses the paths and JSON of a small HTTP API because
  that is how the page's code is written, but the call stays inside the process.
* ``save_redacted(doc_id, filename)`` writes the redacted copy where the user chooses in a *Save As*
  dialog. The document's bytes never pass through the page.

They are closures, not methods of an object handed to the window toolkit: the toolkit resolves dotted
names sent by the page against the object it is given, so an object would let page script walk from
it to anything it references. Every argument is treated as untrusted and checked before use.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import logging
import os
import re
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .service import RedactionService, ServiceError

log = logging.getLogger("redactner.bridge")

DOC = r"/api/v1/documents/(?P<doc_id>[0-9a-f]{32})"
ANALYZE_KEYS = ("labels", "threshold", "deny_terms", "allow_terms")
REDACT_KEYS = ("mode", "exclude_ids", "extra_spans", "analysis_id")
_RESERVED_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})

SavePathChooser = Callable[[str], "Path | None"]


def downloads_folder() -> Path:
    """The user's Downloads folder (which can be relocated on Windows), or the home folder."""
    if os.name == "nt":
        import winreg

        with contextlib.suppress(OSError):
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                folder = Path(winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0])
            if folder.is_dir():
                return folder
    folder = Path.home() / "Downloads"
    return folder if folder.is_dir() else Path.home()


def unused_path(folder: Path, filename: str) -> Path:
    """``folder/filename``, or ``name (2).ext`` and so on when that file already exists."""
    target = folder / filename
    counter = 2
    while target.exists():
        target = folder / f"{Path(filename).stem} ({counter}){Path(filename).suffix}"
        counter += 1
    return target


def safe_docx_name(filename: Any) -> str:
    """A plain ``.docx`` file name: no folders, no characters or device names Windows treats specially."""
    name = Path(str(filename or "").replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    stem = name[:-5] if name.lower().endswith(".docx") else name
    stem = stem.strip(" .")[:120].rstrip(" .")  # Windows drops trailing dots and spaces, so they are not kept
    if not stem or stem.upper() in _RESERVED_NAMES:
        stem = "redacted"
    return f"{stem}.docx"


def _payload_dict(payload: Any, allowed: tuple[str, ...]) -> dict:
    if payload is None:
        return {}
    if not isinstance(payload, dict) or any(key not in allowed for key in payload):
        raise ServiceError(422, "Invalid request: unexpected fields.")
    return payload


def make_bridge(service: RedactionService, choose_save_path: SavePathChooser | None = None,
                save_dir: Path | None = None) -> tuple[Callable[..., dict], Callable[..., dict]]:
    """Build the two functions for the window around ``service``.

    ``choose_save_path(default_name)`` shows the *Save As* dialog and returns the chosen path, or None
    when it is cancelled. With ``save_dir`` set, files go straight into that folder without a dialog.
    Both functions return ``{"status": <HTTP-style code>, "body": <JSON value>}`` and never raise.
    """
    save_dir = Path(save_dir) if save_dir else None

    def dispatch(method: Any, path: Any, payload: Any) -> Any:
        if not isinstance(method, str) or not isinstance(path, str) or len(path) > 200:
            raise ServiceError(400, "Invalid request.")
        method = method.upper()
        if (method, path) == ("GET", "/api/v1/health"):
            return service.health()
        if (method, path) == ("GET", "/api/v1/config"):
            return service.config()
        if (method, path) == ("POST", "/api/v1/documents"):
            fields = _payload_dict(payload, ("base64", "filename"))
            encoded = fields.get("base64")
            if not isinstance(encoded, str) or len(encoded) > service.max_upload_bytes * 4 // 3 + 8:
                raise ServiceError(413, f"File is larger than {service.max_upload_mb} MB.")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ServiceError(400, "The file could not be read.") from exc
            return service.upload(data, safe_docx_name(fields.get("filename")))
        match = re.fullmatch(DOC + r"(?P<action>/analyze|/redact|/report)?", path)
        if match is None:
            raise ServiceError(404, "Unknown request.")
        doc_id, action = match["doc_id"], match["action"]
        if (method, action) == ("POST", "/analyze"):
            return service.analyze(doc_id, **_payload_dict(payload, ANALYZE_KEYS))
        if (method, action) == ("POST", "/redact"):
            return service.redact(doc_id, **_payload_dict(payload, REDACT_KEYS))
        if (method, action) == ("GET", "/report"):
            return service.report(doc_id)
        if (method, action) == ("DELETE", None):
            return service.delete(doc_id)
        raise ServiceError(404, "Unknown request.")

    def answer(work: Callable[[], Any]) -> dict:
        try:
            return {"status": 200, "body": work()}
        except ServiceError as exc:
            return {"status": exc.status, "body": {"detail": exc.detail}}
        except Exception as exc:  # noqa: BLE001 - the page gets a plain message; the log gets where it failed
            # never the error's own text: it can quote the document that caused it
            where = " < ".join(f"{Path(frame.filename).name}:{frame.lineno} in {frame.name}"
                               for frame in reversed(traceback.extract_tb(exc.__traceback__)))
            log.error("request failed with %s at %s", type(exc).__name__, where)
            return {"status": 500, "body": {"detail": "RedactNER hit an unexpected error. Details are in the log file."}}

    def request(method: Any = None, path: Any = None, payload: Any = None) -> dict:
        """Carry one call from the page to the application."""
        return answer(lambda: dispatch(method, path, payload))

    def save(doc_id: Any, filename: Any) -> dict:
        if not isinstance(doc_id, str) or not re.fullmatch(r"[0-9a-f]{32}", doc_id):
            raise ServiceError(404, "Document not found or expired.")
        data = service.redacted_document(doc_id)
        name = safe_docx_name(filename)
        if save_dir is not None:
            save_dir.mkdir(parents=True, exist_ok=True)
            target = unused_path(save_dir, name)
            with open(target, "xb") as handle:  # never replaces a file that appeared in the meantime
                handle.write(data)
        else:
            if choose_save_path is None:
                raise ServiceError(500, "Saving is not available.")
            target = choose_save_path(name)
            if target is None:
                return {"path": None}  # the dialog was cancelled
            target = Path(target)
            if target.suffix.lower() != ".docx":  # a name typed without the extension: add it, never overwrite
                target = unused_path(target.parent, target.name + ".docx")
            try:
                target.write_bytes(data)
            except OSError as exc:
                raise ServiceError(500, f"The file could not be saved ({exc.strerror or 'write failed'}).") from exc
        log.info("redacted document saved")
        return {"path": str(target)}

    def save_redacted(doc_id: Any = None, filename: Any = "redacted.docx") -> dict:
        """Write the redacted copy of a document where the user chooses."""
        return answer(lambda: save(doc_id, filename))

    return request, save_redacted
