"""The application's operations and their input checks (stub model, no GPU needed)."""

import time
from io import BytesIO

import pytest
from conftest import StubModel, stub_detector
from docx import Document

from redactner import service as service_module
from redactner.detector import PIIDetector
from redactner.docx_engine import DocxDocument, extract_text_raw
from redactner.service import (MAX_CONFIDENCE, MAX_EXTRA_SPANS, MAX_TERMS, MIN_CONFIDENCE, DocumentStore, Entry,
                               RedactionService, ServiceError, to_tau)

VALUES = {"Jane Citizen": "PERSON", "jane@example.com": "EMAIL"}
PARAGRAPHS = ["Jane Citizen leads Project Phoenix.", "Write to jane@example.com for access."]


def docx_bytes(paragraphs: list[str] = PARAGRAPHS) -> bytes:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def status_of(call, *args, **kwargs) -> int:
    with pytest.raises(ServiceError) as caught:
        call(*args, **kwargs)
    return caught.value.status


@pytest.fixture
def service():
    built = RedactionService(stub_detector(VALUES))
    yield built
    built.close()


@pytest.fixture
def doc_id(service):
    return service.upload(docx_bytes(), "letter.docx")["doc_id"]


# -- the normal path -------------------------------------------------------------------------------
def test_upload_analyse_redact_and_fetch_the_result(service, doc_id):
    analysis = service.analyze(doc_id)
    assert {(e["text"], e["label"]) for e in analysis["entities"]} == set(VALUES.items())
    assert len(analysis["analysis_id"]) == 32

    result = service.redact(doc_id, mode="label", analysis_id=analysis["analysis_id"])
    assert result["report"]["counts_by_label"] == {"PERSON": 1, "EMAIL": 1}
    assert set(result) == {"report"}  # no download address: the window saves the file itself
    text = extract_text_raw(service.redacted_document(doc_id))
    assert "[PERSON_1]" in text and "[EMAIL_1]" in text and not any(value in text for value in VALUES)
    assert service.report(doc_id) == result["report"]


def test_the_order_of_steps_is_enforced(service, doc_id):
    assert status_of(service.redact, doc_id) == 409             # not analysed yet
    assert status_of(service.redacted_document, doc_id) == 409  # not redacted yet
    assert status_of(service.report, doc_id) == 409
    service.analyze(doc_id)
    service.redact(doc_id)
    service.analyze(doc_id)                                     # a new analysis discards the older redaction
    assert status_of(service.redacted_document, doc_id) == 409


def test_a_review_of_an_older_analysis_is_refused(service, doc_id):
    first = service.analyze(doc_id)["analysis_id"]
    second = service.analyze(doc_id)["analysis_id"]
    assert first != second
    assert status_of(service.redact, doc_id, analysis_id=first) == 409
    assert status_of(service.redacted_document, doc_id) == 409  # the refused request redacted nothing
    service.redact(doc_id, analysis_id=second)
    service.redact(doc_id)                                      # the guard is optional


def test_excluded_detections_stay_and_manual_ones_are_added(service, doc_id):
    analysis = service.analyze(doc_id)
    person = next(e for e in analysis["entities"] if e["label"] == "PERSON")
    first_paragraph = analysis["segments"][0]
    start = first_paragraph["text"].index("Project Phoenix")
    manual = {"segment_id": first_paragraph["id"], "start": start, "end": start + len("Project Phoenix"), "label": "CUSTOM"}
    service.redact(doc_id, mode="mask", exclude_ids=[person["id"]], extra_spans=[manual])
    text = extract_text_raw(service.redacted_document(doc_id))
    assert "Jane Citizen" in text and "Project Phoenix" not in text and "jane@example.com" not in text


def test_the_chosen_confidence_reaches_the_model_as_tau():
    class Recording(StubModel):
        seen = None

        def detect(self, text, threshold=None):
            Recording.seen = threshold
            return super().detect(text, threshold)

    service = RedactionService(PIIDetector(Recording(VALUES)))
    try:
        doc_id = service.upload(docx_bytes())["doc_id"]
        service.analyze(doc_id, threshold=0.03)
        assert Recording.seen == 0.97
        service.analyze(doc_id)
        assert Recording.seen is None  # the tuned thresholds of the model apply
    finally:
        service.close()
    assert to_tau(None) is None and to_tau(0.001) == 0.999


def test_health_and_config_describe_the_model_and_the_limits(service):
    assert service.health() == {"status": "ok", "model": service.model_info}
    config = service.config()
    assert config["model"]["name"] == "stub-model" and config["max_upload_mb"] == 20
    assert (config["min_threshold"], config["max_threshold"]) == (MIN_CONFIDENCE, MAX_CONFIDENCE)
    assert config["default_threshold"] == 0.5 and {"strict", "direct"} <= set(config["presets"])


# -- input checks ----------------------------------------------------------------------------------
@pytest.mark.parametrize("options", [
    {"threshold": 0}, {"threshold": 1}, {"threshold": 0.995}, {"threshold": "0.5"}, {"threshold": True},
    {"threshold": float("nan")},
    {"labels": ["NOT_A_LABEL"]}, {"labels": "PERSON"}, {"labels": [["PERSON"]]},
    {"deny_terms": ["x"] * (MAX_TERMS + 1)}, {"deny_terms": ["x" * 201]}, {"deny_terms": [5]}, {"deny_terms": "term"},
    {"allow_terms": [None]},
])
def test_analyse_refuses_invalid_options(service, doc_id, options):
    assert status_of(service.analyze, doc_id, **options) == 422


@pytest.mark.parametrize("options", [
    {"mode": "pseudonym"}, {"mode": None}, {"mode": ["label"]},
    {"exclude_ids": "e0"}, {"exclude_ids": [0]}, {"exclude_ids": ["x" * 65]},
    {"extra_spans": "all"}, {"extra_spans": [["s", 0, 1, "PERSON"]]},
    {"extra_spans": [{}] * (MAX_EXTRA_SPANS + 1)},
])
def test_redact_refuses_invalid_options(service, doc_id, options):
    service.analyze(doc_id)
    assert status_of(service.redact, doc_id, **options) == 422


@pytest.mark.parametrize("change", [
    {"segment_id": "no-such-paragraph"}, {"segment_id": 7},
    {"start": -1}, {"start": 5, "end": 5}, {"start": 6, "end": 5}, {"end": 10_000},
    {"start": 1.0}, {"start": True}, {"end": "9"},
    {"label": "SECRET"}, {"label": "<img src=x>"}, {"label": None},
])
def test_a_manual_redaction_that_cannot_be_applied_is_an_error(service, doc_id, change):
    analysis = service.analyze(doc_id)
    span = {"segment_id": analysis["segments"][0]["id"], "start": 0, "end": 4, "label": "PERSON", **change}
    assert status_of(service.redact, doc_id, extra_spans=[span]) == 422
    assert status_of(service.redacted_document, doc_id) == 409  # never applied in part


# -- documents that are refused -------------------------------------------------------------------
def test_uploads_are_checked_before_they_are_kept(service, monkeypatch):
    assert status_of(service.upload, b"") == 400
    assert status_of(service.upload, "text, not bytes") == 400
    assert status_of(service.upload, b"<html>not a Word file</html>") == 415
    monkeypatch.setattr(service, "max_upload_bytes", 100)
    assert status_of(service.upload, docx_bytes()) == 413
    assert service.store._items == {}


def test_a_document_with_too_much_text_is_refused_at_upload(service, monkeypatch):
    monkeypatch.setattr(service_module, "MAX_TEXT_CHARS", 40)
    with pytest.raises(ServiceError) as caught:
        service.upload(docx_bytes())
    assert caught.value.status == 413 and "too much text" in caught.value.detail
    assert service.store._items == {}


def test_unknown_and_deleted_documents_are_not_found(service, doc_id):
    for bad in ("0" * 32, "", None, 5, ["x"]):
        assert status_of(service.analyze, bad) == 404
    assert service.delete(doc_id) == {"deleted": True}
    assert service.delete(doc_id) == {"deleted": False}
    assert status_of(service.analyze, doc_id) == 404


def test_file_names_are_kept_short(service):
    assert len(service.upload(docx_bytes(), "n" * 5_000)["filename"]) == 200
    assert service.upload(docx_bytes(), None)["filename"] == "document.docx"


# -- the in-memory store ---------------------------------------------------------------------------
def entry() -> Entry:
    data = docx_bytes()
    return Entry(data=data, filename="letter.docx", doc=DocxDocument.from_bytes(data))


def test_expired_documents_leave_memory_without_another_request():
    store = DocumentStore(ttl_seconds=0.05, sweep_seconds=0.02)
    try:
        store.put(entry())
        deadline = time.time() + 5
        while store._items and time.time() < deadline:  # no call into the store: the timer does the work
            time.sleep(0.02)
        assert store._items == {}
    finally:
        store.close()


def test_a_document_in_use_is_not_forgotten():
    store = DocumentStore(ttl_seconds=0.4, sweep_seconds=0.02)
    try:
        doc_id = store.put(entry())
        for _ in range(12):  # 1.2 s of use, three times the idle limit
            time.sleep(0.1)
            store.get(doc_id)
        deadline = time.time() + 5
        while store._items and time.time() < deadline:
            time.sleep(0.02)
        assert store._items == {}  # and forgotten once it is left alone
    finally:
        store.close()


def test_the_store_keeps_within_its_budget():
    size = entry().size
    store = DocumentStore(budget_bytes=int(size * 2.5))
    try:
        first, second, third = (store.put(entry()) for _ in range(3))
        assert status_of(store.get, first) == 404  # the oldest made room
        assert store.get(second) and store.get(third)
    finally:
        store.close()
    small = DocumentStore(budget_bytes=size - 1)
    try:
        assert status_of(small.put, entry()) == 413
    finally:
        small.close()


def test_closing_the_service_forgets_every_document(service, doc_id):
    service.close()
    assert service.store._items == {}
    assert status_of(service.analyze, doc_id) == 404
