"""Desktop application tests. They use a stub model and never open a window, so they need no GPU or window toolkit."""

import base64
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import stub_detector
from typer.testing import CliRunner

from redactner import desktop, paths
from redactner.cli import app as cli
from redactner.docx_engine import DocxDocument
from redactner.service import RedactionService

SOURCE = Path(desktop.__file__).resolve().parent


# -- file locations ------------------------------------------------------------------------------
def test_model_dir_defaults_to_the_project_and_follows_the_environment(monkeypatch, tmp_path):
    monkeypatch.delenv("REDACTNER_MODEL_DIR", raising=False)
    assert paths.default_model_dir() == paths.bundle_root() / "models" / "redactner-best"
    assert (paths.bundle_root() / "pyproject.toml").exists()
    monkeypatch.setenv("REDACTNER_MODEL_DIR", str(tmp_path))
    assert paths.default_model_dir() == tmp_path


def test_packaged_application_reads_its_bundle(monkeypatch, tmp_path):
    monkeypatch.delenv("REDACTNER_MODEL_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert paths.is_frozen()
    assert paths.default_model_dir() == tmp_path / "models" / "redactner-best"
    assert desktop.ui_dir() == tmp_path / "redactner" / "ui"


# -- pages -----------------------------------------------------------------------------------------
def csp_of(page: str) -> str:
    metas = re.findall(r'<meta http-equiv="Content-Security-Policy" content="([^"]+)">', page)
    assert len(metas) == 1
    return metas[0]


def inline(page: str, tag: str) -> str:
    blocks = re.findall(rf"<{tag}>(.*?)</{tag}>", page, flags=re.DOTALL)
    assert len(blocks) == 1
    return blocks[0]


def sha256_source(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii") + "'"


@pytest.mark.parametrize("build", [desktop.app_page, desktop.loading_page,
                                   lambda: desktop.error_page("it broke", Path("redactner.log")),
                                   lambda: desktop.app_page(allow_eval=True)])
def test_every_page_may_run_only_its_own_script_and_style(build):
    page = build()
    policy = csp_of(page)
    assert policy.startswith("default-src 'none';")  # nothing is loaded and no host is contacted
    assert f"script-src {sha256_source(inline(page, 'script'))}" in policy
    assert f"style-src {sha256_source(inline(page, 'style'))};" in policy
    assert "unsafe-inline" not in policy and "http" not in policy and "*" not in policy and "data:" not in policy.split("img-src")[0]
    assert "base-uri 'none'" in policy and "form-action 'none'" in policy


def test_the_policy_is_strict_on_windows_and_allows_only_the_toolkit_exception_elsewhere():
    assert desktop.toolkit_needs_eval("nt") is False and desktop.toolkit_needs_eval("posix") is True
    strict, relaxed = csp_of(desktop.app_page()), csp_of(desktop.app_page(allow_eval=True))
    assert "unsafe" not in strict
    assert relaxed == strict.replace("; style-src", " 'unsafe-eval'; style-src")  # the one difference


def test_the_interface_is_one_self_contained_document():
    page = desktop.app_page()
    assert "<script src" not in page and 'rel="stylesheet"' not in page and "<iframe" not in page
    assert not re.search(r'\s(?:src|href|action)="(?!data:)[^"]*"', page)  # no address to load or go to
    assert not re.search(r'<[^>]+\son[a-z]+="', page) and ' style="' not in page  # inline handlers and styles would be blocked
    assert "bridgeReady" in page and "pywebview" in page
    assert len(page.encode("utf-16-le")) < 2 * 2**20  # the window toolkit loads at most 2 MB from memory


def test_the_page_script_has_no_way_to_reach_a_network():
    script = (SOURCE / "ui" / "app.js").read_text(encoding="utf-8")
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "importScripts",
                      ".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                      "window.open", "http://", "https://"):
        assert forbidden not in script, forbidden


def test_start_up_pages_escape_their_text(tmp_path):
    assert "Loading the detection model" in desktop.loading_page()
    page = desktop.error_page("<script>alert(1)</script>", tmp_path / "redactner.log")
    assert "alert(1)</script>" not in page and "&lt;script&gt;" in page and "redactner.log" in page


def test_interface_files_that_are_incomplete_stop_the_start(monkeypatch, tmp_path):
    for name in ("index.html", "styles.css", "app.js"):
        (tmp_path / name).write_text("<html></html>", encoding="utf-8")
    monkeypatch.setattr(desktop, "ui_dir", lambda: tmp_path)
    with pytest.raises(RuntimeError):
        desktop.app_page()


# -- the embedded browser --------------------------------------------------------------------------
def test_embedded_browser_switches_are_added_to_harmless_ones(monkeypatch):
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--lang=en-AU")
    desktop.quiet_embedded_browser()
    arguments = os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"].split()
    assert arguments[0] == "--lang=en-AU" and "--disable-background-networking" in arguments
    assert "--proxy-server=http://127.0.0.1:9" in arguments  # a dead end: the browser can reach no host


def test_switches_that_would_open_the_window_to_other_programs_are_dropped(monkeypatch):
    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                       "--remote-debugging-port=9222 --Remote-Allow-Origins=* --user-data-dir=C:\\elsewhere "
                       "--load-extension=C:\\x --disable-web-security --allow-file-access-from-files "
                       "--remote-debugging-pipe --proxy-server=http://203.0.113.5:8080 --no-proxy-server "
                       "--proxy-pac-url=http://203.0.113.5/p.pac --host-resolver-rules=MAP_*_203.0.113.5 --lang=en-AU")
    desktop.quiet_embedded_browser()
    arguments = os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"].split()
    assert arguments == ["--lang=en-AU", *desktop.BROWSER_SWITCHES.split()]


def test_webview2_check_returns_a_boolean():
    assert isinstance(desktop.webview2_available(), bool)


# -- the window's guards ---------------------------------------------------------------------------
class FakeEvent:
    """Stands in for a .NET event: handlers are attached with ``+=``."""

    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


def shown_events():
    loaded = threading.Event()
    loaded.set()
    return SimpleNamespace(loaded=loaded)


def fake_window(url=None):
    control = SimpleNamespace(NavigationStarting=FakeEvent())
    return SimpleNamespace(native=SimpleNamespace(browser=SimpleNamespace(webview=control)), events=shown_events(),
                           get_current_url=lambda: url), control


IN_MEMORY_PAGE = "data:text/html;charset=utf-8;base64,PGgxPg=="  # how the browser reports a page loaded from memory


def navigate(control, address: str = "https://example.com/") -> bool:
    """Fire a navigation; returns True when it was cancelled."""
    args = SimpleNamespace(Cancel=False, Uri=address)
    for handler in control.NavigationStarting.handlers:
        handler(None, args)
    return args.Cancel


def test_only_navigations_the_application_starts_are_allowed():
    window, control = fake_window()
    permit, attached = desktop.lock_navigation(window)
    assert attached
    assert navigate(control) is True                    # a dropped link, a redirect, a script: all cancelled
    assert navigate(control, IN_MEMORY_PAGE) is True    # even an in-memory page, unless the application loads it
    permit()
    assert navigate(control) is True                    # a permission never covers another address
    assert navigate(control, IN_MEMORY_PAGE) is False   # the application loading its own page
    assert navigate(control, IN_MEMORY_PAGE) is True    # one permission covers one navigation


def test_a_missing_browser_control_does_not_stop_the_start():
    permit, attached = desktop.lock_navigation(SimpleNamespace(native=None, events=shown_events()))
    assert not attached  # the caller then guards its functions by the page address
    permit()


def test_the_lock_waits_for_the_window_before_attaching():
    """The browser control exists once the loading page is up; attaching earlier would silently do nothing,
    and would cancel the loading page itself."""
    window, control = fake_window()
    window.events.loaded.clear()
    attached = []
    worker = threading.Thread(target=lambda: attached.append(desktop.lock_navigation(window)))
    worker.start()
    worker.join(0.3)
    assert worker.is_alive() and control.NavigationStarting.handlers == []
    window.events.loaded.set()
    worker.join(5)
    assert not worker.is_alive() and len(control.NavigationStarting.handlers) == 1


def test_the_api_object_given_to_the_toolkit_has_nothing_to_reach():
    sealed = desktop._NoApi()
    assert dir(sealed) == []
    for name in ("__class__", "__dict__", "__init__", "__reduce__", "anything"):
        assert getattr(sealed, name, None) is None


def test_functions_answer_only_while_the_window_shows_the_application():
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"status": 200, "body": "ok"}

    own, _ = fake_window(url=None)  # pages loaded from memory have no address
    assert desktop.on_own_page(own, request)("GET", "/api/v1/health") == {"status": 200, "body": "ok"}
    for address in ("https://example.com/", "file:///C:/page.html", "data:text/html,x"):
        elsewhere, _ = fake_window(url=address)
        assert desktop.on_own_page(elsewhere, request)("GET", "/api/v1/health")["status"] == 403
    assert len(calls) == 1
    assert desktop.on_own_page(own, request).__name__ == "request"  # the name the page calls is kept


# -- no server -------------------------------------------------------------------------------------
def test_the_application_contains_no_server():
    """Nothing listens on a port: no server package and no socket code anywhere in the package."""
    pattern = re.compile(r"^\s*(?:from|import)\s+(fastapi|starlette|uvicorn|socket|socketserver|http\.server|"
                         r"wsgiref|asyncio|aiohttp|flask|bottle|requests|httpx|urllib\.request)\b", re.MULTILINE)
    for path in SOURCE.rglob("*.py"):
        found = pattern.findall(path.read_text(encoding="utf-8"))
        assert not found, f"{path.name} imports {found}"
    assert not (SOURCE / "api").exists()


def test_the_command_line_opens_the_window_and_offers_no_server():
    runner = CliRunner()
    listing = runner.invoke(cli, ["--help"]).output
    assert "desktop" in listing and "serve" not in listing
    assert runner.invoke(cli, ["serve"]).exit_code != 0
    for removed in (["--browser"], ["--port", "8000"]):
        with pytest.raises(SystemExit):
            desktop.parse_args(removed)


# -- self-test -------------------------------------------------------------------------------------
def test_self_test_document_is_a_readable_word_file():
    document = DocxDocument.from_bytes(desktop.self_test_document())
    assert [segment.text for segment in document.segments] == [desktop.SELF_TEST_TEXT]
    assert all(value in desktop.SELF_TEST_TEXT for value in desktop.SELF_TEST_VALUES)


def self_test(monkeypatch, tmp_path, labels: dict) -> tuple[int, dict]:
    monkeypatch.setattr(desktop, "load_service", lambda options: RedactionService(stub_detector(labels)))
    report = tmp_path / "self-test.json"
    options = desktop.parse_args(["--self-test", "--self-test-output", str(report), "--model-dir", str(tmp_path)])
    return desktop.run_self_test(options), json.loads(report.read_text(encoding="utf-8"))


def test_self_test_passes_end_to_end_with_a_stub_model(monkeypatch, tmp_path):
    code, result = self_test(monkeypatch, tmp_path, dict(zip(desktop.SELF_TEST_VALUES, ("PERSON", "PHONE", "EMAIL"))))
    assert code == 0 and result["passed"] and result["page_built"]
    assert result["redactions"] == 3 and result["leaked_values"] == []


def test_self_test_fails_when_the_model_misses_something(monkeypatch, tmp_path):
    code, result = self_test(monkeypatch, tmp_path, {desktop.SELF_TEST_VALUES[0]: "PERSON"})
    assert code == 1 and not result["passed"] and result["redactions"] == 1


# -- the built-in window test ----------------------------------------------------------------------
def test_window_test_script_carries_the_document_and_reports_back():
    script = desktop.probe_script(desktop.self_test_document())
    assert "__DOCUMENT__" not in script and base64.b64encode(desktop.self_test_document()).decode() in script
    assert "probe_report" in script and "eval(" not in script and "new Function" not in script


def test_probe_collects_reports_and_survives_rubbish():
    report, wait = desktop.make_probe()
    assert report.__name__ == "probe_report"  # the name the page calls
    assert wait(0.05) is None
    report(json.dumps({"marks": 3}))
    report("not json")
    report(["a list"])
    report("x" * 200_000)
    assert wait(1) == {"marks": 3}
    assert all("error" in wait(1) for _ in range(3))


class ScriptedWindow:
    """Stands in for the window during a window test: plays the page's part when script is injected."""

    def __init__(self, report, save_dir, *, page, saved_text="Dear Ms [PERSON_1], thank you.", stays=True):
        self.report, self.save_dir, self.page, self.saved_text, self.stays = report, save_dir, page, saved_text, stays
        self.scripts = []

    def run_js(self, script):
        self.scripts.append(script)
        if "window-test.docx" in script:  # the main script: "redact" the document and report
            if self.saved_text is not None:
                from docx import Document

                document = Document()
                document.add_paragraph(self.saved_text)
                document.save(str(self.save_dir / "redacted.docx"))
            if self.page is not None:
                self.report(json.dumps(self.page))
        elif "document.title" in script:
            self.report(json.dumps({"title": "RedactNER", "interface": self.stays}))


GOOD_PAGE = {"step": "done", "marks": 3, "residual": "0 Leaks"}


@pytest.mark.parametrize(
    ("changes", "locked", "passed"),
    [
        ({}, True, True),
        ({}, False, True),                                              # no lock on this system: nothing to check
        ({"stays": False}, True, False),                                # the window let itself be sent elsewhere
        ({"page": {**GOOD_PAGE, "error": "timed out", "step": "waiting for the analysis"}}, True, False),
        ({"page": {**GOOD_PAGE, "marks": 1}}, True, False),             # the model missed most of it
        ({"saved_text": None}, True, False),                            # nothing was saved
        ({"saved_text": "Dear Ms Priya Raman, thank you."}, True, False),  # a value survived in the saved copy
    ],
)
def test_window_test_judges_what_the_page_and_the_saved_file_show(tmp_path, monkeypatch, changes, locked, passed):
    monkeypatch.setattr(desktop.time, "sleep", lambda seconds: None)
    report, wait = desktop.make_probe()
    window = ScriptedWindow(report, tmp_path, **{"page": GOOD_PAGE, **changes})
    result = desktop.window_test(window, locked, wait, tmp_path)
    assert result["passed"] is passed and result["navigation_lock"] is locked
    assert ("stayed_after_navigation_attempt" in result) is locked


def test_window_test_fails_when_the_page_never_answers(tmp_path):
    report, _ = desktop.make_probe()
    window = ScriptedWindow(report, tmp_path, page=None, saved_text=None)
    result = desktop.window_test(window, False, lambda seconds: None, tmp_path)
    assert result["passed"] is False and "did not answer" in result["error"]


def test_window_test_is_a_command_line_switch():
    assert desktop.parse_args(["--window-test"]).window_test is True
    assert desktop.parse_args([]).window_test is False
