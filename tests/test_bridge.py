"""What the page in the window can ask for, and what it cannot (stub model, no window toolkit needed)."""

import base64
import inspect
from io import BytesIO

import pytest
from conftest import stub_detector
from docx import Document

from redactner.bridge import downloads_folder, make_bridge, safe_docx_name, unused_path
from redactner.docx_engine import extract_text_raw
from redactner.service import RedactionService

VALUES = {"Jane Citizen": "PERSON", "jane@example.com": "EMAIL"}
DOCUMENTS = "/api/v1/documents"


def docx_bytes() -> bytes:
    doc = Document()
    doc.add_paragraph("Jane Citizen can be reached at jane@example.com.")
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def as_base64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def service():
    built = RedactionService(stub_detector(VALUES))
    yield built
    built.close()


@pytest.fixture
def bridge(service, tmp_path):
    """The two functions of the window, saving into a folder instead of asking where."""
    return make_bridge(service, save_dir=tmp_path / "out")


def redacted_document(request) -> str:
    """Open, analyse and redact the sample document; returns its id."""
    doc_id = request("POST", DOCUMENTS, {"base64": as_base64(docx_bytes()), "filename": "letter.docx"})["body"]["doc_id"]
    analysis = request("POST", f"{DOCUMENTS}/{doc_id}/analyze", {"labels": ["PERSON", "EMAIL"], "threshold": 0.03})
    assert analysis["status"] == 200
    redaction = request("POST", f"{DOCUMENTS}/{doc_id}/redact",
                        {"mode": "label", "analysis_id": analysis["body"]["analysis_id"]})
    assert redaction["status"] == 200 and redaction["body"]["report"]["counts_by_label"] == {"PERSON": 1, "EMAIL": 1}
    return doc_id


# -- requests ------------------------------------------------------------------------------------
def test_the_page_can_run_the_whole_workflow(bridge, tmp_path):
    request, save_redacted = bridge
    assert request("GET", "/api/v1/health")["body"]["status"] == "ok"
    assert request("get", "/api/v1/config")["body"]["max_upload_mb"] == 20
    doc_id = redacted_document(request)
    assert request("GET", f"{DOCUMENTS}/{doc_id}/report")["body"]["counts_by_label"] == {"PERSON": 1, "EMAIL": 1}

    saved = save_redacted(doc_id, "letter_redacted.docx")
    assert saved == {"status": 200, "body": {"path": str(tmp_path / "out" / "letter_redacted.docx")}}
    text = extract_text_raw((tmp_path / "out" / "letter_redacted.docx").read_bytes())
    assert "[PERSON_1]" in text and not any(value in text for value in VALUES)

    assert request("DELETE", f"{DOCUMENTS}/{doc_id}") == {"status": 200, "body": {"deleted": True}}
    assert request("GET", f"{DOCUMENTS}/{doc_id}/report")["status"] == 404


@pytest.mark.parametrize("method, path, payload, status", [
    ("GET", "/api/v1/nothing", None, 404),
    ("POST", "/api/v1/health", None, 404),                               # wrong method
    ("GET", f"{DOCUMENTS}/{'0' * 32}/download", None, 404),              # the page never receives the file
    ("POST", f"{DOCUMENTS}/../../etc/analyze", None, 404),
    ("POST", f"{DOCUMENTS}/{'A' * 32}/analyze", None, 404),              # ids are lower-case hex only
    ("POST", f"{DOCUMENTS}/{'0' * 32}/analyze/", None, 404),
    ("GET", "/api/v1/health" + "/" * 300, None, 400),
    (None, "/api/v1/health", None, 400),
    ("GET", ["/api/v1/health"], None, 400),
    ("POST", DOCUMENTS, None, 413),                                      # no file at all
    ("POST", DOCUMENTS, {"base64": 5}, 413),
    ("POST", DOCUMENTS, {"base64": "not base64!"}, 400),
    ("POST", DOCUMENTS, {"base64": as_base64(b"plain text")}, 415),
    ("POST", DOCUMENTS, {"base64": "", "path": "C:/Windows/win.ini"}, 422),  # unexpected field
    ("POST", DOCUMENTS, "PK", 422),
])
def test_requests_the_application_does_not_offer_are_refused(bridge, method, path, payload, status):
    request, _ = bridge
    answer = request(method, path, payload)
    assert answer["status"] == status and set(answer["body"]) == {"detail"}


def test_unexpected_fields_never_reach_the_service(bridge):
    request, _ = bridge
    doc_id = request("POST", DOCUMENTS, {"base64": as_base64(docx_bytes())})["body"]["doc_id"]
    assert request("POST", f"{DOCUMENTS}/{doc_id}/analyze", {"doc_id": "x"})["status"] == 422
    assert request("POST", f"{DOCUMENTS}/{doc_id}/analyze", {"__class__": "x"})["status"] == 422
    assert request("POST", f"{DOCUMENTS}/{doc_id}/analyze", {"threshold": 5})["status"] == 422
    assert request("POST", f"{DOCUMENTS}/{doc_id}/redact", {"mode": "label"})["status"] == 409  # not analysed yet


def test_an_upload_larger_than_the_limit_is_refused_before_decoding(service, bridge, monkeypatch):
    request, _ = bridge
    monkeypatch.setattr(service, "max_upload_bytes", 64)
    assert request("POST", DOCUMENTS, {"base64": "A" * 200})["status"] == 413


def test_an_internal_error_is_reported_without_its_details(service, bridge, monkeypatch, caplog):
    request, _ = bridge

    def broken():
        raise RuntimeError("Jane Citizen's document at C:\\Users\\someone\\letter.docx")

    monkeypatch.setattr(service, "health", broken)
    with caplog.at_level("ERROR", logger="redactner.bridge"):
        answer = request("GET", "/api/v1/health")
    assert answer["status"] == 500
    assert "Jane" not in answer["body"]["detail"] and "Users" not in answer["body"]["detail"]
    # the log says what kind of error and where, never the error's text (it can quote the document)
    assert "RuntimeError" in caplog.text and "in broken" in caplog.text
    assert "Jane" not in caplog.text and "letter.docx" not in caplog.text


def test_the_window_gets_two_plain_functions_and_no_object(bridge):
    """Bound methods would hand the window toolkit an object whose attributes page script could walk."""
    for function in bridge:
        assert inspect.isfunction(function) and not hasattr(function, "__self__")
    assert [function.__name__ for function in bridge] == ["request", "save_redacted"]


# -- saving --------------------------------------------------------------------------------------
def test_saving_never_overwrites_and_keeps_only_the_file_name(bridge, tmp_path):
    request, save_redacted = bridge
    doc_id = redacted_document(request)
    first = save_redacted(doc_id, "..\\..\\elsewhere\\report")["body"]["path"]
    second = save_redacted(doc_id, "report.docx")["body"]["path"]
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["report (2).docx", "report.docx"]
    assert first != second and not (tmp_path / "elsewhere").exists()


def test_nothing_is_saved_for_a_document_that_is_not_redacted(bridge, tmp_path):
    request, save_redacted = bridge
    doc_id = request("POST", DOCUMENTS, {"base64": as_base64(docx_bytes())})["body"]["doc_id"]
    assert save_redacted(doc_id)["status"] == 409
    for bad in ("0" * 32, "../../x", None, 5, {"id": doc_id}):
        assert save_redacted(bad)["status"] == 404
    assert not (tmp_path / "out").exists()


def test_save_asks_where_and_reports_a_cancelled_dialog(service, tmp_path):
    asked = []
    choice = [tmp_path / "chosen"]  # typed without the extension

    def choose(default_name):
        asked.append(default_name)
        return choice[0]

    request, save_redacted = make_bridge(service, choose)
    doc_id = redacted_document(request)
    saved = save_redacted(doc_id, "my letter.docx")
    assert saved["body"]["path"] == str(tmp_path / "chosen.docx") and asked == ["my letter.docx"]
    assert "[PERSON_1]" in extract_text_raw((tmp_path / "chosen.docx").read_bytes())

    choice[0] = None  # the dialog was closed
    assert save_redacted(doc_id) == {"status": 200, "body": {"path": None}}
    assert [p.name for p in tmp_path.iterdir()] == ["chosen.docx"]

    choice[0] = tmp_path / "missing folder" / "x.docx"
    failed = save_redacted(doc_id)
    assert failed["status"] == 500 and "could not be saved" in failed["body"]["detail"]


def test_saving_needs_a_dialog_or_a_folder(service):
    request, save_redacted = make_bridge(service)
    assert save_redacted(redacted_document(request))["status"] == 500


@pytest.mark.parametrize("given, expected", [
    ("report.docx", "report.docx"), ("report", "report.docx"), ("Report.DOCX", "Report.docx"),
    ("..\\..\\elsewhere\\report", "report.docx"), ("/etc/passwd", "passwd.docx"), ("C:\\Windows\\win.ini", "win.ini.docx"),
    ('a<b>:c?"|*.docx', "abc.docx"), ("evil\x00na\nme.docx", "evilname.docx"),
    ("CON.docx", "redacted.docx"), ("lpt1", "redacted.docx"), ("  ...  ", "redacted.docx"), (".docx", "redacted.docx"),
    ("", "redacted.docx"), (None, "redacted.docx"), (42, "42.docx"),
])
def test_file_names_from_the_page_are_made_safe(given, expected):
    assert safe_docx_name(given) == expected


def test_long_file_names_are_shortened():
    assert safe_docx_name("n" * 5_000 + ".docx") == "n" * 120 + ".docx"


def test_unused_path_never_overwrites(tmp_path):
    assert unused_path(tmp_path, "redacted.docx").name == "redacted.docx"
    (tmp_path / "redacted.docx").write_bytes(b"x")
    (tmp_path / "redacted (2).docx").write_bytes(b"x")
    assert unused_path(tmp_path, "redacted.docx").name == "redacted (3).docx"


def test_downloads_folder_exists():
    assert downloads_folder().is_dir()
