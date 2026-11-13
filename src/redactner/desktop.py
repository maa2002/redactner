"""Desktop application: the interface in its own window, with no server and no network port.

``redactner-desktop`` (and the packaged ``RedactNER.exe``) show the interface in a native window and
connect it straight to the application inside the same process:

* the page is assembled in memory from the files in ``ui/`` and loaded as a document without an
  address, under a Content-Security-Policy that allows only its own script and style. It cannot fetch
  anything, and there is nothing listening that another program or website could reach;
* the page can call exactly two functions (see :mod:`redactner.bridge`): one carries its requests to
  the application, the other saves the redacted copy through the system's *Save As* dialog;
* the window refuses to navigate anywhere else, so a dropped link or file cannot replace the page;
* a loading screen is shown while the model loads, and closing the window ends the program.

Nothing leaves the machine. The model is read from the bundled files with the Hugging Face libraries
in offline mode. The embedded browser runs in private mode with its own background traffic off, and
any other request it might make is pointed at an address where nothing listens.

``--self-test`` checks an installation end to end (model, Word engine, the page and its bridge)
without opening a window. ``--window-test`` opens the real window, puts a built-in test document
through it the way a person would, and closes again.

The window is drawn by WebView2 on Windows, WebKit on macOS, and GTK WebKit or Qt WebEngine on Linux.
Two protections exist on Windows only, where the engine offers the hooks: the refusal to navigate
and the dead-end address for the engine's own requests. Elsewhere the two functions answer only
while the window shows the application's own page.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import faulthandler
import functools
import hashlib
import html
import importlib.util
import io
import json
import logging
import multiprocessing
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import __version__
from .bridge import downloads_folder, make_bridge
from .paths import APP_NAME, bundle_root, default_model_dir, is_frozen, user_data_dir

log = logging.getLogger("redactner.desktop")

# Evergreen WebView2 Runtime, the browser engine the window is drawn with on Windows
WEBVIEW2_CLIENT_KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://developer.microsoft.com/microsoft-edge/webview2/"

# Switches for the embedded browser. It needs no network at all, so its own background requests are off
# (update checks, experiments, pings) and everything else is sent to a proxy address where nothing
# listens: a request to any host fails on this computer, without even a name lookup leaving it.
BROWSER_SWITCHES = ("--disable-background-networking --disable-component-update --disable-domain-reliability "
                    "--disable-sync --no-pings --dns-prefetch-disable --proxy-server=http://127.0.0.1:9")
# Never inherited from the environment: they would open the window to other programs or redirect its data
UNSAFE_BROWSER_SWITCHES = ("--remote-debugging", "--remote-allow-origins", "--user-data-dir", "--load-extension",
                           "--disable-web-security", "--allow-file-access", "--proxy-", "--no-proxy-server",
                           "--host-resolver-rules", "--host-rules")

# -- self-test material (synthetic) ---------------------------------------------------------------
SELF_TEST_VALUES = ("Priya Raman", "0412 345 678", "priya.raman@example.com.au")
SELF_TEST_TEXT = ("Dear Ms Priya Raman, thank you for your application. Please call 0412 345 678 "
                  "or email priya.raman@example.com.au if any of your details change.")


# ------------------------------------------------------------------------------------------------
# Environment and logging
# ------------------------------------------------------------------------------------------------
def configure_environment() -> None:
    """Keep the model libraries offline and quiet. Must run before they are imported."""
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY", "HF_HUB_DISABLE_PROGRESS_BARS"):
        os.environ[name] = "1"  # forced, not defaulted: the application never needs the network
    os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")


def setup_logging() -> Path:
    """Write log records to a small rotating file; a windowed application has no console to print to.

    The log holds start-up, shutdown and error messages only, never document text or file names.
    """
    folder = user_data_dir() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "redactner.log"
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("pywebview").setLevel(logging.WARNING)
    with contextlib.suppress(Exception):  # if the program dies inside a native library, say where (code lines only)
        faulthandler.enable(file=open(folder / "crash.log", "w", encoding="utf-8"), all_threads=True)  # noqa: SIM115
    if sys.stdout is None or sys.stderr is None:
        # libraries that print would fail on the missing streams of a windowed executable
        console = open(folder / "console.log", "w", buffering=1, encoding="utf-8", errors="replace")  # noqa: SIM115
        sys.stdout = sys.stdout or console
        sys.stderr = sys.stderr or console
    return path


def has_console() -> bool:
    """True when the process was started from a terminal (never for the packaged, windowed application)."""
    return sys.__stdout__ is not None


def message_box(text: str, *, error: bool = False) -> None:
    """A native message box on Windows (plain console output elsewhere)."""
    if os.name == "nt":
        import ctypes

        # MB_ICONERROR or MB_ICONINFORMATION, plus MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, (0x10 if error else 0x40) | 0x10000)
    else:
        print(text, file=sys.__stderr__ or sys.stderr)


def webview2_available() -> bool:
    """Whether the WebView2 Runtime is installed (always True outside Windows, where it is not used)."""
    if os.name != "nt":
        return True
    import winreg

    for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node"), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE"),
                         (winreg.HKEY_CURRENT_USER, "Software")):
        try:
            with winreg.OpenKey(hive, rf"{prefix}\{WEBVIEW2_CLIENT_KEY}") as key:
                version = str(winreg.QueryValueEx(key, "pv")[0])
        except OSError:
            continue
        if version and version != "0.0.0.0":
            return True
    return False


def quiet_embedded_browser() -> None:
    """Fix the embedded browser's switches. Must run before the window is created.

    WebView2 appends the switches in this environment variable to those of the application. The page is
    loaded from memory, so the browser has no reason to contact any host: its own background traffic is
    switched off. Switches inherited from the environment are kept only if they are harmless; one that
    opens a debugging port, for example, would let any local program read the documents in the window.
    """
    inherited = os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "").split()
    kept = [switch for switch in inherited if not switch.lower().startswith(UNSAFE_BROWSER_SWITCHES)]
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = " ".join([*kept, BROWSER_SWITCHES])


# ------------------------------------------------------------------------------------------------
# Pages
# ------------------------------------------------------------------------------------------------
def _csp_hash(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


# Dropped files and links must never replace the page; the interface handles .docx drops itself.
_NO_DROP = "for (const t of ['dragover', 'drop']) window.addEventListener(t, (e) => e.preventDefault());"

_START_STYLE = """
  :root { --bg: #f4f5f7; --text: #171b22; --muted: #5b6474; --track: #e4e7ec; --primary: #2457d6; --danger: #c0262d; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #121418; --text: #e8eaef; --muted: #a1a9b7; --track: #2b3039; --primary: #5b8cff; --danger: #f87171; }
  }
  html, body { height: 100%; margin: 0; }
  body { display: grid; place-items: center; background: var(--bg); color: var(--text);
         font: 15px/1.5 system-ui, "Segoe UI", Roboto, Arial, sans-serif; -webkit-user-select: none; user-select: none; }
  main { width: min(460px, 84vw); text-align: center; }
  h1 { margin: 0 0 6px; font-size: 28px; font-weight: 700; letter-spacing: -0.01em; }
  p { margin: 0; color: var(--muted); }
  .bar { height: 4px; margin: 28px 0 22px; border-radius: 2px; background: var(--track); overflow: hidden; }
  .bar::before { content: ""; display: block; width: 36%; height: 100%; border-radius: 2px; background: var(--primary);
                 animation: slide 1.5s ease-in-out infinite; }
  @keyframes slide { from { transform: translateX(-110%); } to { transform: translateX(290%); } }
  @media (prefers-reduced-motion: reduce) { .bar::before { width: 100%; opacity: 0.55; animation: none; } }
  .small { font-size: 13px; }
  .error { margin: 22px 0 18px; color: var(--text); }
  .error strong { color: var(--danger); }
  code { font: 12.5px ui-monospace, "Cascadia Code", Consolas, monospace; word-break: break-all;
         -webkit-user-select: text; user-select: text; }
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{policy}">
<meta name="color-scheme" content="light dark">
<title>RedactNER</title>
<style>{style}</style>
<script>{script}</script>
</head>
<body>
<main>
<h1>RedactNER</h1>
{body}
</main>
</body>
</html>
"""


def _start_page(body: str) -> str:
    """A start-up page (loading or error). Like the interface, it may run only its own script and style."""
    policy = (f"default-src 'none'; script-src {_csp_hash(_NO_DROP)}; style-src {_csp_hash(_START_STYLE)}; "
              "base-uri 'none'; form-action 'none'")
    return _PAGE.format(policy=policy, style=_START_STYLE, script=_NO_DROP, body=body)


def loading_page() -> str:
    return _start_page('<p>Loading the detection model…</p>\n<div class="bar" role="progressbar" '
                       'aria-label="Loading"></div>\n<p class="small">Everything runs on this computer. '
                       'Your documents are never uploaded.</p>')


def error_page(message: str, log_path: Path) -> str:
    return _start_page(f'<p class="error"><strong>RedactNER could not start.</strong><br>{html.escape(message)}</p>\n'
                       f'<p class="small">Details are in the log file:<br><code>{html.escape(str(log_path))}</code></p>')


def ui_dir() -> Path:
    """The interface files: beside this module in a checkout, inside the bundle when packaged."""
    return (bundle_root() / "redactner" / "ui") if is_frozen() else Path(__file__).resolve().parent / "ui"


def toolkit_needs_eval(platform: str = os.name) -> bool:
    """Whether the window toolkit's own page script must be allowed to build functions from text.

    The toolkit creates the page's two functions, and hands results back, with ``new Function`` and
    ``eval``. WebView2 on Windows exempts script that the host program adds. The WebKit and Qt engines
    used on macOS and Linux apply the page's policy to it, so the policy has to allow that there.
    Loading or adding any other script stays forbidden on every system.
    """
    return platform != "nt"


def app_page(allow_eval: bool = False) -> str:
    """The interface as one self-contained document.

    The script and the style sheet are placed inside the page, and the Content-Security-Policy names
    them by their hashes: nothing else can run, and the page can load or contact nothing at all.
    ``allow_eval`` adds the one exception described in :func:`toolkit_needs_eval`.
    """
    folder = ui_dir()
    page = (folder / "index.html").read_text(encoding="utf-8")
    style = (folder / "styles.css").read_text(encoding="utf-8").replace("</style", "<\\/style")
    script = (folder / "app.js").read_text(encoding="utf-8").replace("</script", "<\\/script")
    link, tag = '<link rel="stylesheet" href="styles.css">', '<script src="app.js" defer></script>'
    if page.count(link) != 1 or page.count(tag) != 1:
        raise RuntimeError("The interface files are incomplete.")
    scripts = _csp_hash(script) + (" 'unsafe-eval'" if allow_eval else "")
    policy = (f"default-src 'none'; script-src {scripts}; style-src {_csp_hash(style)}; img-src data:; "
              "base-uri 'none'; form-action 'none'")
    head = f'<meta http-equiv="Content-Security-Policy" content="{policy}">\n  <style>{style}</style>'
    return page.replace(link, head).replace(tag, f"<script>{script}</script>")


# ------------------------------------------------------------------------------------------------
# The window
# ------------------------------------------------------------------------------------------------
def load_service(options: argparse.Namespace):
    """Load the model and wrap it in the application's service (the slow part of start-up)."""
    from .pipeline import build_detector
    from .service import RedactionService

    with contextlib.suppress(Exception):  # cosmetic: no progress bars in the log
        from transformers.utils import logging as transformers_logging

        transformers_logging.disable_progress_bar()
    started = time.perf_counter()
    service = RedactionService(build_detector(options.model_dir, options.device))
    log.info("model loaded in %.1f s", time.perf_counter() - started)
    return service


def window_size(webview) -> tuple[int, int]:
    """A comfortable default that still fits a small laptop screen."""
    width, height = 1280, 860
    with contextlib.suppress(Exception):
        screens = webview.screens() if callable(webview.screens) else webview.screens
        width, height = min(width, int(screens[0].width * 0.92)), min(height, int(screens[0].height * 0.88))
    return max(width, 960), max(height, 640)


def lock_navigation(window):
    """Cancel every navigation except the application's own; returns ``(permit, attached)``.

    The application only ever shows pages it built in memory. Calling ``permit()`` lets the next such
    page load; anything else a script, a dropped link or a redirect tries is cancelled, so the window
    never shows another page. ``attached`` is False where the browser control has no such hook
    (another platform or toolkit version); the caller then guards its functions by the page address.
    """
    permitted = threading.Event()

    def on_navigation_starting(_sender, args) -> None:
        if permitted.is_set() and str(args.Uri).startswith("data:text/html"):  # how an in-memory page is loaded
            permitted.clear()
        else:
            args.Cancel = True
            log.warning("blocked a navigation away from the interface")

    try:
        window.events.loaded.wait(30)  # the loading page is up, so the browser control exists
        window.native.browser.webview.NavigationStarting += on_navigation_starting
    except Exception as exc:  # noqa: BLE001 - no such control
        log.log(logging.WARNING if os.name == "nt" else logging.INFO,
                "navigation lock not attached (%s); functions are guarded by the page address", type(exc).__name__)
        return (lambda: None), False
    log.info("navigation lock attached")
    return permitted.set, True


class _NoApi:
    """Given to the window toolkit as the page's API object: it has no attribute at all.

    The toolkit looks up names sent by the page on this object, following dots. With nothing to find,
    the only things the page can call are the two functions registered by name in :func:`run_window`.
    """

    __slots__ = ()

    def __getattribute__(self, name: str):
        raise AttributeError(name)

    def __dir__(self) -> list[str]:
        return []


def on_own_page(window, function):
    """Run ``function`` only while the window shows a page of the application.

    Those pages are loaded from memory and have no address; a page with one is not ours.
    """

    @functools.wraps(function)
    def call(*args):
        if window.get_current_url() not in (None, "", "about:blank"):
            return {"status": 403, "body": {"detail": "Not available."}}
        return function(*args)

    return call


# What the window test runs inside the page: it waits for the application, opens the built-in test
# document through the page's own file input, presses Redact & Download and reports what happened.
_PROBE_SCRIPT = """
(async function () {
  var out = {};
  function until(test, ms) {
    return new Promise(function (resolve, reject) {
      var end = Date.now() + ms;
      (function check() {
        var ok = false;
        try { ok = test(); } catch (e) { ok = false; }
        if (ok) resolve(); else if (Date.now() > end) reject(new Error('timed out')); else setTimeout(check, 100);
      })();
    });
  }
  try {
    out.step = 'waiting for the functions of the application';
    await until(function () { return window.pywebview && window.pywebview.api && window.pywebview.api.probe_report; }, 60000);
    out.step = 'waiting for the model';
    await until(function () { return document.getElementById('status-model').dataset.state === 'ok'; }, 60000);
    out.model = document.getElementById('status-model-text').textContent.trim();
    out.address = String(location.href);
    out.step = 'opening the document';
    var raw = atob('__DOCUMENT__');
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    var transfer = new DataTransfer();
    transfer.items.add(new File([bytes], 'window-test.docx'));
    var input = document.getElementById('file-input');
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    out.step = 'waiting for the analysis';
    await until(function () {
      return !document.getElementById('btn-redact').disabled && document.querySelectorAll('.rd').length >= 3;
    }, 180000);
    out.marks = document.querySelectorAll('.rd').length;
    out.step = 'redacting and saving';
    document.getElementById('btn-redact').click();
    await until(function () {
      return Array.prototype.some.call(document.querySelectorAll('#toasts .toast-msg'),
                                       function (e) { return e.textContent.indexOf('Saved ') === 0; });
    }, 120000);
    out.residual = document.getElementById('residual-value').textContent.trim();
    out.step = 'done';
  } catch (e) {
    out.error = String(e && e.message ? e.message : e);
  }
  window.pywebview.api.probe_report(JSON.stringify(out));
})();
"""
_PROBE_STILL_HERE = ("window.pywebview.api.probe_report(JSON.stringify({title: document.title, "
                     "interface: !!document.getElementById('btn-redact')}));")


def probe_script(document: bytes) -> str:
    return _PROBE_SCRIPT.replace("__DOCUMENT__", base64.b64encode(document).decode("ascii"))


def make_probe() -> tuple[Callable[..., dict], Callable[[float], dict | None]]:
    """The function the page reports to during a window test, and a function that waits for a report."""
    reports: queue.Queue[dict] = queue.Queue()

    def probe_report(text: Any = None) -> dict:
        data = None
        if isinstance(text, str) and len(text) < 100_000:
            with contextlib.suppress(ValueError):
                data = json.loads(text)
        reports.put(data if isinstance(data, dict) else {"error": "the page sent a report that could not be read"})
        return {"status": 200, "body": None}

    def wait(seconds: float) -> dict | None:
        try:
            return reports.get(timeout=seconds)
        except queue.Empty:
            return None

    return probe_report, wait


def window_test(window, locked: bool, wait_for_report: Callable[[float], dict | None], save_dir: Path) -> dict:
    """Use the real window once: open the built-in document through the page, redact it and check the saved copy."""
    from .docx_engine import find_residuals

    result: dict = {"navigation_lock": locked, "passed": False}
    log.info("window test: sending the test script to the page")
    window.run_js(probe_script(self_test_document()))
    log.info("window test: waiting for the page to report")
    page = wait_for_report(420)
    log.info("window test: the page %s", "reported" if page is not None else "did not answer")
    if page is None:
        result["error"] = "the page did not answer; its script or its connection to the application is not working"
        return result
    result["page"] = page
    saved = sorted(save_dir.glob("*.docx"))
    result["saved_files"] = len(saved)
    leaked = sorted({f["value"] for path in saved for f in find_residuals(path.read_bytes(), SELF_TEST_VALUES)})
    result["leaked_values"] = leaked
    passed = "error" not in page and page.get("marks", 0) >= 3 and len(saved) == 1 and not leaked
    if locked:  # a page script tries to send the window elsewhere; it must still show the application afterwards
        window.run_js("setTimeout(function () { location.href = 'https://example.invalid/'; }, 10);")
        time.sleep(2)
        window.run_js(_PROBE_STILL_HERE)
        after = wait_for_report(20)
        result["stayed_after_navigation_attempt"] = bool(after and after.get("interface"))
        passed = passed and result["stayed_after_navigation_attempt"]
    result["passed"] = bool(passed)
    return result


def browser_engine() -> str | None:
    """The engine the window is drawn with: WebView2 on Windows, Qt WebEngine on Linux when it is installed
    (the ``desktop`` extra installs it), otherwise whatever the window toolkit finds (WebKit on macOS)."""
    if os.name == "nt":
        return "edgechromium"
    if sys.platform.startswith("linux") and importlib.util.find_spec("PyQt6") is not None:
        return "qt"
    return None


def run_window(options: argparse.Namespace, log_path: Path) -> int:
    """Show the interface in a native window; returns when the window is closed.

    With ``--window-test`` the window tests itself (see :func:`window_test`) and closes again.
    """
    import webview

    quiet_embedded_browser()
    webview.settings["ALLOW_DOWNLOADS"] = False   # the application saves files itself
    webview.settings["ALLOW_FILE_URLS"] = False   # the page is loaded from memory, never from disk
    # a request for a new window is never handed to the web browser; it becomes a navigation, which is refused
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = False
    width, height = window_size(webview)
    window = webview.create_window(APP_NAME, html=loading_page(), js_api=_NoApi(), width=width, height=height,
                                   min_size=(960, 640), text_select=True, background_color="#f4f5f7")
    closed, failed = threading.Event(), threading.Event()
    window.events.closed += closed.set
    save_dialog = webview.FileDialog.SAVE
    testing = bool(getattr(options, "window_test", False))
    test_result: dict = {"version": __version__, "packaged": is_frozen(), "passed": False}
    probe_report, wait_for_report = make_probe()
    save_dir = options.save_dir
    scratch_dir = None
    if testing and save_dir is None:  # a test never asks where to save
        save_dir = scratch_dir = Path(tempfile.mkdtemp(prefix="redactner-window-test-"))

    def choose_save_path(default_name: str) -> Path | None:
        chosen = window.create_file_dialog(save_dialog, directory=str(downloads_folder()), save_filename=default_name,
                                           file_types=("Word Document (*.docx)",))
        if not chosen:
            return None
        return Path(chosen if isinstance(chosen, str) else chosen[0])

    def boot() -> None:
        permit_navigation, locked = lock_navigation(window)
        # with the lock on, the window can only ever show the application's own pages
        guard = (lambda function: function) if locked else functools.partial(on_own_page, window)
        try:
            service = load_service(options)
            request, save_redacted = make_bridge(service, choose_save_path, save_dir)
            page = app_page(allow_eval=toolkit_needs_eval())
            if closed.is_set():
                return
            functions = [guard(request), guard(save_redacted)]
            if testing:
                functions.append(probe_report)
            window.expose(*functions)
            window.events.loaded.clear()
            permit_navigation()
            # "about:blank" as the page's address: without it some engines report the program's folder
            window.load_html(page, "about:blank")
            log.info("interface loaded")
            if testing:
                log.info("window test: page loaded: %s", window.events.loaded.wait(60))
                test_result.update(window_test(window, locked, wait_for_report, save_dir))
        except Exception as exc:  # noqa: BLE001 - every start-up failure is reported in the window
            log.exception("start-up failed")
            failed.set()
            test_result["error"] = f"{type(exc).__name__}: {exc}"
            with contextlib.suppress(Exception):
                permit_navigation()
                window.load_html(error_page(str(exc) or type(exc).__name__, log_path), "about:blank")
        if testing:
            with contextlib.suppress(Exception):
                window.destroy()

    webview.start(boot, gui=browser_engine(), private_mode=True)
    if not testing:
        return 1 if failed.is_set() else 0
    if scratch_dir is not None:
        shutil.rmtree(scratch_dir, ignore_errors=True)  # the test's own folder with its one redacted test file
    summary = json.dumps(test_result, indent=2)
    log.info("window test %s\n%s", "passed" if test_result["passed"] else "FAILED", summary)
    if options.self_test_output:
        Path(options.self_test_output).write_text(summary, encoding="utf-8")
    if has_console():
        print(summary, flush=True)
    return 0 if test_result["passed"] else 1


def self_test_document() -> bytes:
    """A minimal Word document holding the self-test sentence."""
    word_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    parts = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?>'
                               '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                               '<Default Extension="xml" ContentType="application/xml"/>'
                               '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
                               'officedocument.wordprocessingml.document.main+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?>'
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                       'relationships/officeDocument" Target="word/document.xml"/></Relationships>',
        "word/document.xml": f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{word_ns}"><w:body><w:p><w:r>'
                             f'<w:t xml:space="preserve">{html.escape(SELF_TEST_TEXT)}</w:t></w:r></w:p></w:body></w:document>',
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def run_self_test(options: argparse.Namespace) -> int:
    """Redact a synthetic document through the same path the window uses and check that nothing leaks."""
    from .docx_engine import find_residuals

    result: dict = {"version": __version__, "packaged": is_frozen(), "passed": False}
    service = None
    try:
        service = load_service(options)
        request, _save = make_bridge(service)
        page = app_page()
        result["model"] = request("GET", "/api/v1/health")["body"]["model"]
        result["page_built"] = "Content-Security-Policy" in page and "bridgeReady" in page
        upload = request("POST", "/api/v1/documents", {"base64": base64.b64encode(self_test_document()).decode("ascii"),
                                                       "filename": "self-test.docx"})
        doc_id = upload["body"]["doc_id"]
        analysis = request("POST", f"/api/v1/documents/{doc_id}/analyze", {})["body"]
        redaction = request("POST", f"/api/v1/documents/{doc_id}/redact",
                            {"mode": "label", "analysis_id": analysis["analysis_id"]})
        result["redactions"] = sum(redaction["body"]["report"]["counts_by_label"].values())
        redacted = service.redacted_document(doc_id)
        result["leaked_values"] = sorted({finding["value"] for finding in find_residuals(redacted, SELF_TEST_VALUES)})
        result["passed"] = bool(result["page_built"] and result["redactions"] >= 3 and not result["leaked_values"])
    except Exception as exc:  # noqa: BLE001 - a failed check must end as a failed self-test
        log.exception("self-test error")
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if service is not None:
            service.close()
    summary = json.dumps(result, indent=2)
    log.info("self-test %s\n%s", "passed" if result["passed"] else "FAILED", summary)
    if options.self_test_output:
        Path(options.self_test_output).write_text(summary, encoding="utf-8")
    if has_console():
        print(summary, flush=True)
    return 0 if result["passed"] else 1


# ------------------------------------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="redactner-desktop", description="RedactNER desktop application.")
    parser.add_argument("--model-dir", type=Path, default=None, help="fine-tuned checkpoint directory")
    parser.add_argument("--device", choices=("cpu", "cuda"), default=None,
                        help="compute device (detected automatically by default)")
    parser.add_argument("--save-dir", type=Path, default=None,
                        help="save redacted copies into this folder without asking where")
    parser.add_argument("--self-test", action="store_true",
                        help="check the model, the Word engine and the interface, then exit (0 = passed)")
    parser.add_argument("--window-test", action="store_true",
                        help="open the window, put a built-in test document through it, then close (0 = passed)")
    parser.add_argument("--self-test-output", type=Path, default=None,
                        help="write the result of --self-test or --window-test to this JSON file")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    options = parser.parse_args(argv)
    options.model_dir = options.model_dir or default_model_dir()
    return options


def run(argv: list[str] | None = None) -> int:
    log_path = setup_logging()
    configure_environment()
    options = parse_args(argv)
    log.info("%s %s starting (packaged: %s)", APP_NAME, __version__, is_frozen())

    def stop(text: str) -> int:
        """Tell the person why the window cannot open; a window test records the reason and moves on."""
        if options.window_test:
            summary = json.dumps({"version": __version__, "packaged": is_frozen(), "passed": False, "error": text}, indent=2)
            log.info("window test FAILED\n%s", summary)
            if options.self_test_output:
                Path(options.self_test_output).write_text(summary, encoding="utf-8")
            if has_console():
                print(summary, flush=True)
        else:
            message_box(text, error=True)
        return 1

    try:
        if options.self_test:
            return run_self_test(options)
        if not webview2_available():
            return stop("RedactNER shows its window with the Microsoft Edge WebView2 Runtime, which is not installed "
                        f"on this computer.\n\nInstall it from Microsoft, then start RedactNER again:\n{WEBVIEW2_DOWNLOAD}")
        try:
            return run_window(options, log_path)
        except ImportError:
            log.exception("the window toolkit is not available")
            return stop("The window toolkit (pywebview) is not installed.\n\nInstall it with:  uv sync --extra desktop")
    except Exception as exc:  # noqa: BLE001 - last resort: tell the user instead of closing silently
        log.exception("fatal error")
        return stop(f"RedactNER could not start.\n\n{exc}\n\nDetails are in the log file:\n{log_path}")


def main(argv: list[str] | None = None) -> None:
    multiprocessing.freeze_support()
    code = run(argv)
    log.info("exit code %d", code)
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    # End now. A thread may still be loading the model, or waiting to hand a result to a window that has
    # already closed (on some systems that never returns). Nothing needs saving, so nothing is waited for.
    os._exit(code)


if __name__ == "__main__":
    main()
