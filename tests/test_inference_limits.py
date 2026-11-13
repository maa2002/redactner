"""Model metadata ranges, hostile text handling and whole-word snapping on long runs (no model is loaded)."""

import time

import pytest

from redactner.inference import (MAX_TEXT_CHARS, TokenClassifierDetector, clean_text, load_tokenizer, snap_to_words,
                                 validate_meta)
from redactner.paths import default_model_dir
from redactner.types import Span


def test_the_tokenizer_is_read_exactly_as_it_was_saved_with_the_model():
    """The saved pipeline turns non-breaking and other special spaces into ordinary ones, as in training.
    A tokenizer rebuilt by another version of the model library may not, and the model would see unknown tokens."""
    if not (default_model_dir() / "tokenizer.json").exists():
        pytest.skip("the exported model is not in this checkout")
    tokenizer = load_tokenizer(default_model_dir(), 512, 128)
    tokens = tokenizer.encode("Jane Citizen met at 5 pm").tokens
    assert tokens == ["[CLS]", "▁Jane", "▁Citizen", "▁met", "▁at", "▁5", "▁pm", "[SEP]"]

    encoding = tokenizer.encode("word " * 2000)  # a long text is cut into overlapping windows
    windows = [encoding, *encoding.overflowing]
    assert len(windows) > 1 and all(len(window.ids) <= 512 for window in windows)
    first_end, second_start = windows[0].offsets[-2][1], windows[1].offsets[1][0]
    assert second_start < first_end  # the second window starts before the first one ends


def test_a_model_folder_without_its_tokenizer_file_is_refused(tmp_path):
    with pytest.raises(FileNotFoundError, match="tokenizer.json"):
        load_tokenizer(tmp_path, 512, 128)


def test_valid_meta_passes():
    validate_meta({"threshold": 0.97, "category_thresholds": None, "max_len": 512, "stride": 128})
    validate_meta({})
    validate_meta({"threshold": 0.5, "category_thresholds": {"PERSON": 0.9}, "max_len": 16, "stride": 0})


@pytest.mark.parametrize("meta, field", [
    ({"threshold": 0}, "threshold"), ({"threshold": 0.49}, "threshold"), ({"threshold": 1}, "threshold"),
    ({"threshold": "0.9"}, "threshold"), ({"threshold": True}, "threshold"),
    ({"category_thresholds": {"PERSON": 0.0}}, "category_thresholds.PERSON"),
    ({"category_thresholds": {"URL": 1.0}}, "category_thresholds.URL"),
    ({"category_thresholds": [0.9]}, "category_thresholds"),
    ({"max_len": 15}, "max_len"), ({"max_len": 8193}, "max_len"), ({"max_len": 512.5}, "max_len"),
    ({"max_len": 512, "stride": -1}, "stride"), ({"max_len": 512, "stride": 512}, "stride"),
    ({"max_len": 64, "stride": 100}, "stride"),
])
def test_bad_meta_names_the_field(meta, field):
    with pytest.raises(ValueError, match=field):
        validate_meta(meta)


def test_lone_surrogates_replaced_one_for_one():
    text = "ab\ud800cd\udfffe"
    cleaned = clean_text(text)
    assert cleaned == "ab�cd�e" and len(cleaned) == len(text)
    cleaned.encode("utf-8")  # no longer raises
    assert clean_text("plain text é") == "plain text é"


def test_detect_refuses_oversized_text():
    detector = TokenClassifierDetector.__new__(TokenClassifierDetector)  # no model: the check comes first
    with pytest.raises(ValueError, match="too long to analyse"):
        detector.detect("a" * (MAX_TEXT_CHARS + 1))


def test_snap_covers_the_whole_word_however_long():
    text = "x" * 1000  # e.g. a long key: leaving any of it visible would be a leak
    (sp,) = snap_to_words(text, [Span(500, 501, "PERSON", 0.9)])
    assert (sp.start, sp.end) == (0, 1000)
    assert snap_to_words("Smithson", [Span(0, 3, "PERSON")])[0].end == 8


def test_snap_stays_fast_with_many_spans_in_one_long_run():
    text = "x" * 200_000 + " tail"
    spans = [Span(i, i + 1, "CREDENTIAL", 0.9) for i in range(0, 200_000, 20)]  # 10,000 spans, one run
    started = time.perf_counter()
    snapped = snap_to_words(text, spans)
    assert time.perf_counter() - started < 5  # walking outwards per span would take minutes
    assert [(sp.start, sp.end) for sp in snapped] == [(0, 200_000)]  # all merged into the one whole word
