"""Follow-up merge-conflict resolution.

Uses two real local git repos (bare "origin" + working clone) so the flow
exercises the same git plumbing the bot uses against GitHub. Signing is
disabled per-repo because the test fixture's SSH key is a stub.
"""
from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from github_client import GitHubInstance, RepositoryRef
from llm.base import FileChange, MergeConflictContext, MergeResolution
from processor import _contains_conflict_markers, _resolve_merge_conflicts
from slack import SlackNotifier


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo_with_main(work: Path, origin: Path, *, file_content: str) -> None:
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True, text=True,
    )
    # Seed main with one file.
    seed = work / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)], check=True, capture_output=True, text=True)
    _configure_signing_off(seed)
    (seed / "hello.txt").write_text(file_content, encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "init", cwd=seed)
    _git("remote", "add", "origin", str(origin), cwd=seed)
    _git("push", "origin", "main", cwd=seed)


def _configure_signing_off(repo: Path) -> None:
    """The fixture's stub SSH key can't sign — disable it locally."""
    _git("config", "user.name", "tester", cwd=repo)
    _git("config", "user.email", "tester@example.com", cwd=repo)
    _git("config", "commit.gpgsign", "false", cwd=repo)


def _advance_main(seed: Path, origin: Path, *, new_content: str) -> None:
    """Push a conflicting change to origin/main."""
    (seed / "hello.txt").write_text(new_content, encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "main: change hello", cwd=seed)
    _git("push", "origin", "main", cwd=seed)


def _make_feature_branch(work: Path, origin: Path, *, branch: str, content: str) -> Path:
    repo = work / "feature"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", "main", str(origin), str(repo)],
        check=True, capture_output=True, text=True,
    )
    _configure_signing_off(repo)
    _git("checkout", "-b", branch, cwd=repo)
    (repo / "hello.txt").write_text(content, encoding="utf-8")
    _git("add", ".", cwd=repo)
    _git("commit", "-m", "feat: change hello on branch", cwd=repo)
    _git("push", "origin", branch, cwd=repo)
    return repo


@pytest.fixture
def instance() -> GitHubInstance:
    return GitHubInstance.github_com(
        pat="pat", author_name="tester", author_email="tester@example.com",
    )


@pytest.fixture
def base_repo() -> RepositoryRef:
    return RepositoryRef(owner="octo-org", repo="octo-repo")


@pytest.fixture
def slack() -> SlackNotifier:
    return SlackNotifier(token=None, channel=None)


def test_no_conflicts_no_op(tmp_path, settings, stub_provider, instance, base_repo, slack):
    """Base hasn't advanced since clone → merge is already up-to-date → no commit, no push."""
    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="hi\n")
    repo = _make_feature_branch(tmp_path, origin, branch="feat", content="hi from feat\n")
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    s = replace(settings, merge_conflict_resolution=True, dry_run=False)

    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=stub_provider,
        settings=s,
        slack=slack,
    )

    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after == head_before, "should not have created a merge commit"
    assert stub_provider.merge_calls == [], "LLM must not be invoked when there's no conflict"


def test_clean_merge_after_base_advances(
    tmp_path, settings, stub_provider, instance, base_repo, slack
):
    """Base advances on a different file → fast-forward / clean merge → commit + push, LLM not called."""
    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="hi\n")
    repo = _make_feature_branch(tmp_path, origin, branch="feat", content="hi from feat\n")

    # Advance main on an unrelated file so the merge is clean.
    seed = tmp_path / "seed"
    (seed / "other.txt").write_text("new file on main\n", encoding="utf-8")
    _git("add", ".", cwd=seed)
    _git("commit", "-m", "main: add other.txt", cwd=seed)
    _git("push", "origin", "main", cwd=seed)

    s = replace(settings, merge_conflict_resolution=True, dry_run=False)
    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=stub_provider,
        settings=s,
        slack=slack,
    )

    log = _git("log", "--oneline", cwd=repo).stdout
    assert "chore(merge)" in log, f"expected merge commit, got:\n{log}"
    assert stub_provider.merge_calls == [], "LLM not needed for clean merge"


def test_conflict_resolved_by_llm(
    tmp_path, settings, instance, base_repo, slack
):
    """Real conflict on hello.txt → LLM returns resolved content → merge commit + push."""
    from conftest import StubProvider

    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="line1\nline2\n")
    repo = _make_feature_branch(
        tmp_path, origin, branch="feat", content="line1\nline2 from feat\n"
    )
    _advance_main(tmp_path / "seed", origin, new_content="line1\nline2 from main\n")

    from llm.base import Decision  # local import to avoid noise at module top

    provider = StubProvider(
        Decision(decision="dismiss", reply="n/a"),
        merge_resolution=MergeResolution(
            decision="resolve",
            reason="kept feat side; behaviour change wins",
            commit_message="fix(merge): resolve hello.txt with feat side",
            files=[FileChange(path="hello.txt", content="line1\nline2 from feat\n")],
        ),
    )

    s = replace(settings, merge_conflict_resolution=True, dry_run=False)
    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=provider,
        settings=s,
        slack=slack,
    )

    assert len(provider.merge_calls) == 1
    ctx = provider.merge_calls[0]
    assert ctx.base_branch == "main"
    assert ctx.head_branch == "feat"
    assert [f.path for f in ctx.conflicted_files] == ["hello.txt"]
    assert "<<<<<<<" in ctx.conflicted_files[0].content

    log = _git("log", "--oneline", cwd=repo).stdout
    assert "fix(merge): resolve hello.txt with feat side" in log
    # File on disk should be the resolved content, not the markered one.
    assert (repo / "hello.txt").read_text() == "line1\nline2 from feat\n"


def test_conflict_aborted_by_llm(
    tmp_path, settings, instance, base_repo, slack
):
    """LLM says abort → merge --abort, no commit, no push, working tree clean."""
    from conftest import StubProvider
    from llm.base import Decision

    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="line1\nline2\n")
    repo = _make_feature_branch(
        tmp_path, origin, branch="feat", content="line1\nline2 from feat\n"
    )
    _advance_main(tmp_path / "seed", origin, new_content="line1\nline2 from main\n")
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    provider = StubProvider(
        Decision(decision="dismiss", reply="n/a"),
        merge_resolution=MergeResolution(
            decision="abort", reason="too ambiguous, leaving for human"
        ),
    )

    s = replace(settings, merge_conflict_resolution=True, dry_run=False)
    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=provider,
        settings=s,
        slack=slack,
    )

    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after == head_before
    # Working tree should be clean after the abort.
    status = _git("status", "--porcelain", cwd=repo).stdout
    assert status == ""


def test_marker_detector_flags_real_markers_only():
    """Detector must catch real `<<<<<<<`/`=======`/`>>>>>>>` lines without
    false-positiving on prose that happens to contain that many of those
    characters but no trailing space/EOL (e.g. `<<<<<<<some`)."""
    assert _contains_conflict_markers("a\n<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> main\n")
    assert _contains_conflict_markers("=======\n")
    assert not _contains_conflict_markers("plain content with no markers\n")
    assert not _contains_conflict_markers("<<<<<<<no-space-or-eol-after-this")


def test_marker_in_llm_response_aborts(
    tmp_path, settings, instance, base_repo, slack
):
    """If the LLM echoes conflict markers back as 'resolved' content, abort
    the merge — never push a markered file."""
    from conftest import StubProvider
    from llm.base import Decision

    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="line1\nline2\n")
    repo = _make_feature_branch(
        tmp_path, origin, branch="feat", content="line1\nline2 from feat\n"
    )
    _advance_main(tmp_path / "seed", origin, new_content="line1\nline2 from main\n")
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    provider = StubProvider(
        Decision(decision="dismiss", reply="n/a"),
        merge_resolution=MergeResolution(
            decision="resolve",
            reason="LLM got confused",
            commit_message="fix(merge): resolve",
            files=[FileChange(
                path="hello.txt",
                # Model echoes conflict markers back in the "resolved" content.
                content="line1\n<<<<<<< HEAD\nline2 from feat\n=======\nline2 from main\n>>>>>>> main\n",
            )],
        ),
    )

    s = replace(settings, merge_conflict_resolution=True, dry_run=False)
    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=provider,
        settings=s,
        slack=slack,
    )

    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after == head_before, "markered content must never be pushed"
    status = _git("status", "--porcelain", cwd=repo).stdout
    assert status == ""


def test_llm_partial_resolution_aborts(
    tmp_path, settings, instance, base_repo, slack
):
    """LLM returns content for only some conflicted files → abort the merge,
    never push a half-resolved state."""
    from conftest import StubProvider
    from llm.base import Decision

    origin = tmp_path / "origin.git"
    _init_repo_with_main(tmp_path, origin, file_content="line1\nline2\n")
    repo = _make_feature_branch(
        tmp_path, origin, branch="feat", content="line1\nline2 from feat\n"
    )
    _advance_main(tmp_path / "seed", origin, new_content="line1\nline2 from main\n")
    head_before = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    provider = StubProvider(
        Decision(decision="dismiss", reply="n/a"),
        merge_resolution=MergeResolution(
            decision="resolve",
            reason="claimed resolved",
            commit_message="fix(merge): resolve",
            # Returns NO files even though decision is resolve — should be treated as partial/abort.
            files=[],
        ),
    )

    s = replace(settings, merge_conflict_resolution=True, dry_run=False)
    _resolve_merge_conflicts(
        repo_dir=repo,
        instance=instance,
        base_repo=base_repo,
        head_repo=base_repo,
        head_branch="feat",
        base_branch="main",
        pr_number=1,
        provider=provider,
        settings=s,
        slack=slack,
    )

    head_after = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head_after == head_before, "must not push a half-resolved merge"
    status = _git("status", "--porcelain", cwd=repo).stdout
    assert status == ""
