"""Large model files are stored in parts and joined back byte for byte."""

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
