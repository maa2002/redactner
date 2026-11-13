# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the RedactNER desktop application: one folder, no console window.

Run it from the project root in the build environment (see ``packaging/build.ps1``)::

    pyinstaller packaging/redactner.spec --noconfirm

The result is ``dist/RedactNER/RedactNER.exe`` with everything it needs beside it in ``_internal``: the
Python runtime, the CPU build of PyTorch, the interface and the exported model. The model's large
weight file is stored in parts in the repository; it is joined here, so the packaged application never
has to write into its own folder.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from redactner import __version__  # noqa: E402
from redactner.weights import PARTS_FILE, join_split_files  # noqa: E402

# -- data files ----------------------------------------------------------------------------------
MODEL_DIR = ROOT / "models" / "redactner-best"
join_split_files(MODEL_DIR)  # rebuilds and checksums the joined weight file if it is missing


def packaged(name: str) -> bool:
    """Model files the application reads at run time (not the parts, their manifest or the training log)."""
    return ".part" not in name and name not in (PARTS_FILE, ".gitignore", "training_summary.json")


datas = [(str(path), "models/redactner-best") for path in sorted(MODEL_DIR.iterdir())
         if path.is_file() and packaged(path.name)]
datas.append((str(ROOT / "src" / "redactner" / "ui"), "redactner/ui"))  # the interface: read into memory at start

# -- code ----------------------------------------------------------------------------------------
# The model classes are imported by name at run time, which the import scan cannot see.
hiddenimports = collect_submodules("transformers.models.deberta_v2") + collect_submodules("transformers.models.auto")

# Never used by the application, so never packaged, even if the build environment happens to have them:
# server libraries, the model hub's download helper, test tools, icon drawing, notebooks.
# (The model libraries import a network client library and command-line helpers themselves, so those are
# packaged; the application keeps the libraries in offline mode and never uses them.)
excludes = ["fastapi", "starlette", "uvicorn", "pydantic", "pydantic_core", "hf_xet",
            "PIL", "docx", "faker", "pytest", "_pytest", "tkinter", "IPython", "matplotlib", "pandas"]

analysis = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "src")],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
# Some hooks copy the metadata folders of installed packages; that of the excluded test tool is not wanted either.
analysis.datas = [entry for entry in analysis.datas if not entry[0].lower().startswith("pytest-")]
pyz = PYZ(analysis.pure)

# -- Windows file properties ---------------------------------------------------------------------
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32 import versioninfo as vi

    numbers = tuple(int(part) for part in (__version__.split(".") + ["0", "0", "0"])[:4])
    strings = [
        vi.StringStruct("CompanyName", "RedactNER Project"),
        vi.StringStruct("FileDescription", "RedactNER"),
        vi.StringStruct("FileVersion", __version__),
        vi.StringStruct("InternalName", "RedactNER"),
        vi.StringStruct("LegalCopyright", "Released under the MIT License"),
        vi.StringStruct("OriginalFilename", "RedactNER.exe"),
        vi.StringStruct("ProductName", "RedactNER"),
        vi.StringStruct("ProductVersion", __version__),
    ]
    version_info = vi.VSVersionInfo(
        ffi=vi.FixedFileInfo(filevers=numbers, prodvers=numbers),
        kids=[vi.StringFileInfo([vi.StringTable("040904B0", strings)]),
              vi.VarFileInfo([vi.VarStruct("Translation", [1033, 1200])])],
    )

exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="RedactNER",
    console=False,  # a windowed application: messages go to the log file instead of a terminal
    icon=str(ROOT / "packaging" / "icon.ico"),
    version=version_info,
    upx=False,
)
COLLECT(exe, analysis.binaries, analysis.datas, name="RedactNER", upx=False)
