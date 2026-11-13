"""Write THIRD-PARTY-NOTICES.txt for the packaged application: the licences of everything it includes.

Run in the build environment (``packaging/build.ps1`` does this)::

    python packaging/notices.py dist/RedactNER/THIRD-PARTY-NOTICES.txt

The packages are the ones pinned in ``requirements.txt``, without those used only to build. Each
package's own licence files are copied in, so the notices always match the versions that were packaged.
"""

from __future__ import annotations

import re
import sys
from importlib import metadata
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD_ONLY = {"pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "pefile", "pywin32-ctypes", "pillow"}
LICENCE_FILE = re.compile(r"(^|/)(licen[cs]e|copying|notice|authors)[^/]*$", re.IGNORECASE)
RULE = "=" * 100

INTRO = """RedactNER: third-party notices

RedactNER itself is released under the MIT License (see LICENSE.txt). The application includes the
software and the model listed below, which remain under their own licences.

Model
  The detection model was fine-tuned from microsoft/deberta-v3-base (MIT License, Microsoft) on
  synthetic data: NVIDIA Nemotron-PII (CC BY 4.0), ai4privacy OpenPII (CC BY 4.0) and Gretel Synthetic
  PII Finance (Apache License 2.0).

Runtime
  Python {python} (Python Software Foundation License Version 2).
  The window is drawn by the Microsoft Edge WebView2 Runtime installed on the computer; it is not part
  of this package. The WebView2 SDK libraries that come with pywebview are distributed by Microsoft
  under the licence terms of the WebView2 SDK.
"""


def packaged_names() -> list[str]:
    names = []
    for line in (HERE / "requirements.txt").read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line)
        if match and match[1].lower() not in BUILD_ONLY:
            names.append(match[1])
    return sorted(names, key=str.lower)


def licence_name(meta) -> str:
    """The licence as the package states it: the expression, the short field, or its classifiers."""
    stated = meta.get("License-Expression") or meta.get("License") or ""
    if not stated or len(stated) > 80 or "\n" in stated:  # some packages put the whole text in this field
        classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
        stated = "; ".join(classifiers)
    return stated or "see the licence text below"


def licence_texts(dist) -> list[tuple[str, str]]:
    texts = []
    for file in dist.files or []:
        path = file.as_posix()
        if ".dist-info/" in path and LICENCE_FILE.search(path.split(".dist-info/", 1)[1]):
            with_text = Path(str(dist.locate_file(file)))
            if with_text.is_file():
                texts.append((path.split(".dist-info/", 1)[1], with_text.read_text(encoding="utf-8", errors="replace").strip()))
    return texts


def build() -> str:
    version = ".".join(str(part) for part in sys.version_info[:3])
    sections = [INTRO.format(python=version)]
    for name in packaged_names():
        dist = metadata.distribution(name)
        meta = dist.metadata
        home = meta.get("Home-page") or next((u.split(",", 1)[1].strip() for u in meta.get_all("Project-URL") or []
                                              if u.lower().startswith(("homepage", "home", "source", "repository"))), "")
        head = [RULE, f"{meta['Name']} {dist.version}", f"Licence: {licence_name(meta)}"] + ([f"Website: {home}"] if home else [])
        body = [f"\n--- {title} ---\n{text}" for title, text in licence_texts(dist)]
        sections.append("\n".join(head + [RULE] + (body or ["\n(The package does not include a separate licence file.)"])))
    return "\n\n".join(sections) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1])
    text = build()
    target.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {target} ({len(packaged_names())} packages, {len(text) // 1024} KB).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
