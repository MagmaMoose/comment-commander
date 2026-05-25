"""Trigger-error summaries for `TriggerResult.error`.

Pre-fix, `/process` recorded only `type(exc).__name__`, so the runs UI
surfaced bare strings like "CalledProcessError" with no clue at which git
step failed. These tests pin the new behaviour: subprocess errors carry
their redacted command + stderr, generic exceptions carry their message,
PATs are scrubbed, and the output is length-bounded.
"""
from __future__ import annotations

import subprocess

from processor import summarize_exception


def test_summarize_called_process_error_includes_cmd_and_stderr():
    exc = subprocess.CalledProcessError(
        returncode=128,
        cmd=["git", "clone", "https://x-access-token:secret-pat@github.com/o/r", "/tmp/x"],
        output="",
        stderr="fatal: Authentication failed for 'https://github.com/o/r'\n",
    )
    summary = summarize_exception(exc)
    assert "CalledProcessError" in summary
    assert "rc=128" in summary
    assert "Authentication failed" in summary
    # PAT redaction must apply.
    assert "secret-pat" not in summary
    assert "x-access-token:***" in summary


def test_summarize_called_process_error_falls_back_to_stdout():
    """Some git commands (notably `git push`) write rejection messages to
    stdout, not stderr. Don't lose them."""
    exc = subprocess.CalledProcessError(
        returncode=1,
        cmd=["git", "push", "origin", "HEAD:refs/heads/feat"],
        output="! [remote rejected] feat -> feat (protected branch)\n",
        stderr="",
    )
    summary = summarize_exception(exc)
    assert "protected branch" in summary


def test_summarize_generic_exception_includes_message():
    summary = summarize_exception(RuntimeError("disk is full"))
    assert summary == "RuntimeError: disk is full"


def test_summarize_truncates_overlong_output():
    huge = "x" * 5000
    exc = subprocess.CalledProcessError(returncode=1, cmd=["git", "x"], stderr=huge)
    summary = summarize_exception(exc)
    assert len(summary) <= 500
    assert summary.endswith("…")
