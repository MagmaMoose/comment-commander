"""Conventional Commits normalisation in processor._commit_subject."""
from __future__ import annotations

import pytest

from llm.base import Decision
from processor import _commit_subject


def _decision(message: str | None) -> Decision:
    return Decision(decision="fix", reply="", commit_message=message, files=[])


@pytest.mark.parametrize(
    "raw",
    [
        "fix: address null check",
        "feat(api): add new endpoint",
        "chore(deps): bump foo to 1.2.3",
        "refactor!: drop deprecated method",
        "docs: clarify README",
        "ci: pin runner image",
    ],
)
def test_already_conventional_is_preserved(raw):
    assert _commit_subject(_decision(raw)) == raw


def test_prefixes_fix_when_type_missing():
    out = _commit_subject(_decision("Pin mikrotik-minder Helm chart to 0.1.0"))
    assert out == "fix: pin mikrotik-minder Helm chart to 0.1.0"


def test_prefixes_fix_when_type_unknown():
    out = _commit_subject(_decision("WIP: random thoughts"))
    assert out == "fix: wIP: random thoughts" or out.startswith("fix:")


def test_strips_trailing_period():
    out = _commit_subject(_decision("fix: add null check."))
    assert out == "fix: add null check"


def test_uses_first_line_only():
    out = _commit_subject(_decision("fix: add null check\n\nlonger body here"))
    assert out == "fix: add null check"


def test_empty_message_falls_back_to_generic_fix():
    assert _commit_subject(_decision(None)) == "fix: address PR review feedback"
    assert _commit_subject(_decision("")) == "fix: address PR review feedback"


def test_lowercases_first_letter_after_prefix():
    out = _commit_subject(_decision("Add input validation"))
    assert out.startswith("fix: ")
    assert out[5].islower()
