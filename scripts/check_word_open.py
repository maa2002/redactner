"""Open .docx files in the locally installed Microsoft Word (via COM, invisibly) to prove they are valid.

A file that Word cannot open, or would need to repair, is a corrupt redaction. Also prints the text
Word itself extracts, an extraction path independent of our own DOCX reader (used for leak checks).

    uv run python scripts/check_word_open.py samples/*_redacted.docx [--gold samples/x.gold.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def word_text(path: Path) -> str:
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    word = win32com.client.DispatchEx("Word.Application")
    word.Visible = False
    word.DisplayAlerts = 0
    try:
        doc = word.Documents.Open(str(path.resolve()), False, True, False)  # ConfirmConversions, ReadOnly, AddToRecent
        parts = [doc.Content.Text]
        for section in doc.Sections:
            for hf in list(section.Headers) + list(section.Footers):
                parts.append(hf.Range.Text)
        parts += [c.Range.Text for c in doc.Comments]
        doc.Close(False)
        return "\n".join(parts)
    finally:
        word.Quit()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--gold", type=Path, help="gold sidecar JSON; checks that no gold value survives")
    args = ap.parse_args()
    failures = 0
    for path in args.files:
        try:
            text = word_text(path)
        except Exception as exc:  # noqa: BLE001 - COM errors are opaque; report and continue
            print(f"FAIL  {path.name}: Word could not open it ({exc})")
            failures += 1
            continue
        leaks = []
        if args.gold and args.gold.exists():
            from redactner.labels import PRESETS  # gold types the default (strict) preset redacts

            gold = json.loads(args.gold.read_text(encoding="utf-8"))
            values = [g["value"] for g in gold if g["label"] in PRESETS["strict"]]
            leaks = [v for v in values if len(v) >= 3 and v.lower() in text.lower()]
        status = "OK  " if not leaks else "LEAK"
        failures += bool(leaks)
        print(f"{status}  {path.name}: opened in Word, {len(text):,} chars" + (f", leaked: {leaks}" if leaks else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
