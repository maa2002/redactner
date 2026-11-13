"""Large model files are stored in parts and joined back byte for byte."""

import json
import os

import pytest

from redactner.weights import join_split_files, split_large_files


def test_split_and_join_round_trip(tmp_path):
    big = os.urandom(2500)
    (tmp_path / "model.safetensors").write_bytes(big)
    (tmp_path / "config.json").write_text("{}")
    assert split_large_files(tmp_path, max_bytes=1000) == ["model.safetensors"]
    assert not (tmp_path / "model.safetensors").exists()
    assert sorted(p.name for p in tmp_path.glob("*.part*")) == [f"model.safetensors.part0{i}" for i in range(3)]
    assert (tmp_path / ".gitignore").read_text() == "/model.safetensors\n"
    assert join_split_files(tmp_path) == ["model.safetensors"]
    assert (tmp_path / "model.safetensors").read_bytes() == big
    assert join_split_files(tmp_path) == []  # already joined


def test_join_detects_corrupted_parts(tmp_path):
    (tmp_path / "w.bin").write_bytes(os.urandom(300))
    split_large_files(tmp_path, max_bytes=100)
    (tmp_path / "w.bin.part01").write_bytes(b"x" * 100)
    with pytest.raises(OSError):
        join_split_files(tmp_path)


def _write_manifest(folder, manifest):
    (folder / "parts.json").write_text(json.dumps(manifest), encoding="utf-8")


def _entry(parts, size=1):
    return {"parts": parts, "sha256": "0" * 64, "bytes": size}


@pytest.mark.parametrize("key", ["../evil.bin", "..", ".", "/abs/evil.bin", "C:\\evil.bin", "sub/evil.bin", "a\\b"])
def test_join_rejects_traversal_keys(tmp_path, key):
    folder = tmp_path / "model"
    folder.mkdir()
    _write_manifest(folder, {key: _entry([f"{key}.part00"])})
    with pytest.raises(OSError):
        join_split_files(folder)
    assert not (tmp_path / "evil.bin").exists()


@pytest.mark.parametrize("part", ["../secret.txt", "/etc/passwd", "w.bin.part00/../../x", "other.part00", "C:\\x"])
def test_join_rejects_part_names_outside_folder(tmp_path, part):
    folder = tmp_path / "model"
    folder.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    _write_manifest(folder, {"w.bin": _entry([part])})
    with pytest.raises(OSError):
        join_split_files(folder)
    assert not (folder / "w.bin").exists()


@pytest.mark.parametrize("raw", ["{not json", "[]", '{"w.bin": 3}', '{"w.bin": {"parts": ["w.bin.part00"]}}',
                                 '{"w.bin": {"parts": [], "sha256": "x", "bytes": 1}}',
                                 '{"w.bin": {"parts": "w.bin.part00", "sha256": "x", "bytes": 1}}'])
def test_join_malformed_manifest_is_one_oserror(tmp_path, raw):
    (tmp_path / "parts.json").write_text(raw, encoding="utf-8")
    with pytest.raises(OSError, match="parts.json"):
        join_split_files(tmp_path)


def test_join_cleans_temporary_file_after_failure(tmp_path):
    (tmp_path / "w.bin").write_bytes(os.urandom(300))
    split_large_files(tmp_path, max_bytes=100)
    (tmp_path / "w.bin.part01").write_bytes(b"x" * 100)  # checksum failure
    with pytest.raises(OSError):
        join_split_files(tmp_path)
    (tmp_path / "w.bin.part02").unlink()  # missing part failure
    with pytest.raises(OSError):
        join_split_files(tmp_path)
    assert list(tmp_path.glob("*.tmp")) == [] and not (tmp_path / "w.bin").exists()


def test_join_keeps_fast_path_for_complete_file(tmp_path):
    (tmp_path / "w.bin").write_bytes(os.urandom(300))
    split_large_files(tmp_path, max_bytes=100)
    assert join_split_files(tmp_path) == ["w.bin"]
    for part in tmp_path.glob("*.part*"):
        part.unlink()  # a complete joined file must not need the parts again
    assert join_split_files(tmp_path) == []
