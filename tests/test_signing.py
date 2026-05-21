"""SSH signing smoke test.

Generates a real ed25519 keypair, installs it via signing.install_ssh_signing_key,
and signs/verifies a payload with ssh-keygen. Skips if ssh-keygen isn't available
on the test host.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from signing import SigningError, configure_repo_signing, install_ssh_signing_key

if shutil.which("ssh-keygen") is None:
    pytest.skip("ssh-keygen not available", allow_module_level=True)


def _generate_unencrypted_ed25519(tmp_path: Path) -> str:
    key_path = tmp_path / "src_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_path)],
        check=True,
        capture_output=True,
    )
    return key_path.read_text(encoding="utf-8")


def test_install_writes_key_with_secure_perms(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    private_key = _generate_unencrypted_ed25519(tmp_path)
    installed = install_ssh_signing_key(private_key, home=str(tmp_path))
    assert installed.exists()
    assert installed.stat().st_mode & 0o777 == 0o600


def test_install_rejects_garbage_key(tmp_path: Path):
    with pytest.raises(SigningError):
        install_ssh_signing_key("not a real key", home=str(tmp_path))


def test_signs_and_verifies_payload(tmp_path: Path):
    private_key = _generate_unencrypted_ed25519(tmp_path)
    installed = install_ssh_signing_key(private_key, home=str(tmp_path))
    public_key = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(installed)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    payload = tmp_path / "msg"
    payload.write_text("hello world\n")
    subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-n", "git", "-f", str(installed), str(payload)],
        check=True,
        capture_output=True,
    )
    signature = payload.with_suffix(".sig")
    assert signature.exists()

    # Build allowed_signers and verify.
    allowed_signers = tmp_path / "allowed_signers"
    allowed_signers.write_text(f"signer@example.com {public_key}\n")
    result = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(allowed_signers),
            "-I",
            "signer@example.com",
            "-n",
            "git",
            "-s",
            str(signature),
        ],
        input=payload.read_text(),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_configure_repo_signing_writes_git_config(tmp_path: Path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    configure_repo_signing(
        tmp_path,
        signing_key_path="/path/to/key",
        author_name="CalebSargeant",
        author_email="caleb@example.com",
    )
    config = subprocess.run(
        ["git", "-C", str(tmp_path), "config", "--list", "--local"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "user.signingkey=/path/to/key" in config
    assert "gpg.format=ssh" in config
    assert "commit.gpgsign=true" in config
    assert "user.email=caleb@example.com" in config
