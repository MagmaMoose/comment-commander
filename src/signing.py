"""SSH signing setup.

Mirrors `~/.gitconfig`:
    [gpg]
        format = ssh
    [commit]
        gpgsign = true

The OpenSSH private key (no passphrase) is written to disk at startup with
0600 perms; per-clone `git config` points `user.signingkey` at that path so
that `git commit -S` invokes `ssh-keygen -Y sign` against the key file
directly — no ssh-agent involvement.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class SigningError(RuntimeError):
    pass


def install_ssh_signing_key(private_key: str, *, home: str | None = None) -> Path:
    """Persist the SSH signing private key and return its path."""
    ssh_dir = Path(home or os.path.expanduser("~")) / ".ssh"
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        ssh_dir.chmod(0o700)
    except PermissionError:  # pragma: no cover - in tests against tmp dirs
        pass

    key_path = ssh_dir / "comment_commander_signing"
    body = private_key if private_key.endswith("\n") else private_key + "\n"
    key_path.write_text(body, encoding="utf-8")
    key_path.chmod(0o600)

    # Sanity-check: ssh-keygen -y reads a private key (no agent involvement)
    # and prints the public half. Failing now is much better than failing
    # halfway through a commit. Persist that public half to <path>.pub —
    # `git commit -S` with gpg.format=ssh requires it sitting next to the
    # private key, otherwise ssh-keygen returns:
    #   "Couldn't load public key <signingkey>: No such file or directory"
    result = subprocess.run(
        ["ssh-keygen", "-y", "-f", str(key_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SigningError(
            f"ssh-keygen could not read the signing key: {result.stderr.strip()}"
        )
    pub_path = key_path.parent / (key_path.name + ".pub")
    pub_path.write_text(result.stdout if result.stdout.endswith("\n") else result.stdout + "\n",
                        encoding="utf-8")
    pub_path.chmod(0o644)
    logger.info("SSH signing key installed at %s (public half at %s)", key_path, pub_path)
    return key_path


def configure_repo_signing(
    repo_dir: str | Path,
    signing_key_path: str | Path,
    author_name: str,
    author_email: str,
) -> None:
    """Set git config inside a cloned repo so `git commit -S` works."""
    pairs = [
        ("user.name", author_name),
        ("user.email", author_email),
        ("user.signingkey", str(signing_key_path)),
        ("gpg.format", "ssh"),
        ("commit.gpgsign", "true"),
        ("tag.gpgsign", "false"),
    ]
    for key, value in pairs:
        subprocess.run(
            ["git", "-C", str(repo_dir), "config", key, value],
            check=True,
            capture_output=True,
        )
