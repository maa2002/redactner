"""Pipeline behaviour that the UI's live preview relies on (stub model, no GPU needed)."""

from io import BytesIO

from conftest import stub_detector
from docx import Document

from redactner.docx_engine import DocxDocument, extract_text_raw
from redactner.pipeline import RedactionPipeline


def make_doc(paragraphs: list[str]) -> DocxDocument:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = BytesIO()
    doc.save(buf)
    return DocxDocument.from_bytes(buf.getvalue())


def test_label_numbering_follows_document_order():
    doc = make_doc(["Write to b@second.com today.", "Or a@first.com.", "Again b@second.com please."])
    pipe = RedactionPipeline(stub_detector({"b@second.com": "EMAIL", "a@first.com": "EMAIL"}))
    analysis = pipe.analyze(doc)
    out, _ = pipe.redact(doc, analysis["entities"], mode="label")
    text = extract_text_raw(out)
    assert text.index("[EMAIL_1]") < text.index("[EMAIL_2]")
    assert text.count("[EMAIL_1]") == 2 and "b@second.com" not in text and "a@first.com" not in text


def test_kept_value_survives_safety_net():
    doc = make_doc(["Contact jane@example.com for access.", "Escalations: jane@example.com."])
    pipe = RedactionPipeline(stub_detector({"jane@example.com": "EMAIL"}))
    entities = pipe.analyze(doc)["entities"]
    first = next(e for e in entities if e["segment_id"] == doc.segments[0].id)
    out, _ = pipe.redact(doc, entities, mode="mask", exclude_ids=[first["id"]])
    text = extract_text_raw(out)
    assert text.count("jane@example.com") == 1  # kept once, redacted once
    assert "█████" in text


def test_always_and_never_redact_lists():
    doc = make_doc(["Project Phoenix is led by Jane Citizen.", "Contact Jane Citizen at UTS."])
    pipe = RedactionPipeline(stub_detector({"Jane Citizen": "PERSON", "UTS": "ORGANIZATION"}))
    found = {(e["text"], e["label"]) for e in pipe.analyze(doc, deny_terms=["project phoenix"],
                                                            labels=["PERSON", "ORGANIZATION"],
                                                            allow_terms=["UTS"])["entities"]}
    assert ("Project Phoenix", "CUSTOM") in found
    assert ("Jane Citizen", "PERSON") in found
    assert not any(text == "UTS" for text, _ in found)


def test_analysis_segments_carry_layout_fields():
    doc = make_doc(["Plain paragraph with 0412 345 678."])
    seg = RedactionPipeline(stub_detector({"0412 345 678": "PHONE"})).analyze(doc)["segments"][0]
    assert {"id", "location", "text"} <= set(seg)
