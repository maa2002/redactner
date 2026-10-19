"""Pipeline behaviour that the UI's live preview relies on (rules-only detector, no GPU needed)."""

from io import BytesIO

from docx import Document

from redactner.docx_engine import DocxDocument, extract_text_raw
from redactner.pipeline import RedactionPipeline, build_detectors


def make_doc(paragraphs: list[str]) -> DocxDocument:
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buf = BytesIO()
    doc.save(buf)
    return DocxDocument.from_bytes(buf.getvalue())


def test_label_numbering_follows_document_order():
    doc = make_doc(["Write to b@second.com today.", "Or a@first.com.", "Again b@second.com please."])
    pipe = RedactionPipeline(build_detectors(None))
    analysis = pipe.analyze(doc, detector="rules")
    out, _ = pipe.redact(doc, analysis["entities"], mode="label")
    text = extract_text_raw(out)
    assert text.index("[EMAIL_1]") < text.index("[EMAIL_2]")
    assert text.count("[EMAIL_1]") == 2 and "b@second.com" not in text and "a@first.com" not in text


def test_kept_value_survives_safety_net():
    doc = make_doc(["Contact jane@example.com for access.", "Escalations: jane@example.com."])
    pipe = RedactionPipeline(build_detectors(None))
    entities = pipe.analyze(doc, detector="rules")["entities"]
    first = next(e for e in entities if e["segment_id"] == doc.segments[0].id)
    out, _ = pipe.redact(doc, entities, mode="mask", exclude_ids=[first["id"]])
    text = extract_text_raw(out)
    assert text.count("jane@example.com") == 1  # kept once, redacted once
    assert "█████" in text


def test_analysis_segments_carry_layout_fields():
    doc = make_doc(["Plain paragraph with 0412 345 678."])
    seg = RedactionPipeline(build_detectors(None)).analyze(doc, detector="rules")["segments"][0]
    assert {"id", "location", "text"} <= set(seg)
