"""The fix routine asks the LLM for each changed file's *full contents*. A model
asked to regurgitate a large file routinely truncates it, so a truncated render
must be refused — never written over the real file. Regression test for the
incident where a 3,780-line file was replaced with an 8-line fragment.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from llm.base import FileChange
from processor import (
    TruncatedReplacementError,
    _apply_files,
    _looks_truncated,
)


def _big(lines: int) -> str:
    return "\n".join(f"line {i}" for i in range(lines))


def test_looks_truncated_flags_collapse_of_a_large_file():
    existing = _big(100)
    assert _looks_truncated(existing, _big(8)) is True   # the real incident shape
    assert _looks_truncated(existing, "") is True         # blanked
    assert _looks_truncated(existing, "   \n  ") is True   # whitespace only


def test_looks_truncated_allows_real_edits_and_small_files():
    existing = _big(100)
    assert _looks_truncated(existing, _big(98)) is False  # ordinary small edit
    assert _looks_truncated(existing, _big(60)) is False  # 60% retained
    # A small file may legitimately shrink a lot — only large files are guarded.
    assert _looks_truncated(_big(10), "x = 1") is False


def test_apply_files_refuses_truncation_and_preserves_the_file(tmp_path: Path):
    f = tmp_path / "index.ts"
    original = _big(3780)
    f.write_text(original, encoding="utf-8")

    with pytest.raises(TruncatedReplacementError):
        _apply_files(tmp_path, [FileChange(path="index.ts", content=_big(8))])

    # The real file is untouched — no partial or destructive write happened.
    assert f.read_text(encoding="utf-8") == original


def test_apply_files_aborts_the_whole_set_before_writing_anything(tmp_path: Path):
    good = tmp_path / "small.py"
    good.write_text("a = 1\n", encoding="utf-8")
    big = tmp_path / "big.py"
    big.write_text(_big(100), encoding="utf-8")

    # One truncated file in the set must abort ALL writes (no partial apply),
    # even when a legitimate change is listed before it.
    with pytest.raises(TruncatedReplacementError):
        _apply_files(
            tmp_path,
            [
                FileChange(path="small.py", content="a = 2\n"),  # legitimate
                FileChange(path="big.py", content=_big(3)),       # truncated
            ],
        )
    assert good.read_text(encoding="utf-8") == "a = 1\n"  # untouched
    assert big.read_text(encoding="utf-8") == _big(100)   # not destroyed


def test_apply_files_allows_a_legitimate_edit(tmp_path: Path):
    f = tmp_path / "mod.py"
    f.write_text(_big(100), encoding="utf-8")
    applied = _apply_files(tmp_path, [FileChange(path="mod.py", content=_big(98))])
    assert [c.path for c in applied] == ["mod.py"]
    assert f.read_text(encoding="utf-8") == _big(98)


def test_apply_files_allows_new_file_creation(tmp_path: Path):
    applied = _apply_files(tmp_path, [FileChange(path="new.py", content="x = 1\n")])
    assert [c.path for c in applied] == ["new.py"]
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "x = 1\n"
