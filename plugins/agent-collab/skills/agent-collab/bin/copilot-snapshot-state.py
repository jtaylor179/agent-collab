#!/usr/bin/env python3
"""Fingerprint the ordinary Git state copied by copilot-exec.sh.

The fingerprint is deliberately limited to the state that the adapter can
faithfully replay: HEAD, staged and unstaged binary diffs, and Git-visible
untracked regular files or symlinks. Paths are handled as bytes so unusual
filenames remain unambiguous.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys


class UnsupportedState(RuntimeError):
    """The repository is outside the adapter's ordinary-state contract."""


def _git(repo: str, *args: str) -> bytes:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", repo, *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise UnsupportedState(
            f"git {' '.join(args)} failed"
            + (f": {detail}" if detail else "")
        )
    return completed.stdout


def _frame(digest: "hashlib._Hash", label: bytes, value: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def _reject_unsupported_status(repo: str, status_output: bytes) -> None:
    if _git(repo, "ls-files", "--unmerged", "-z"):
        raise UnsupportedState(
            "COPILOT_READONLY=1 does not support repositories with unmerged entries"
        )

    for record in status_output.split(b"\0"):
        if not record.startswith((b"1 ", b"2 ")):
            continue
        field_limit = 8 if record.startswith(b"1 ") else 9
        fields = record.split(b" ", field_limit)
        if len(fields) <= 4:
            raise UnsupportedState("could not parse Git porcelain-v2 status")
        xy = fields[1]
        index_mode = fields[4]
        if xy == b".A" and index_mode == b"000000":
            raise UnsupportedState(
                "COPILOT_READONLY=1 does not support intent-to-add entries"
            )


def fingerprint(repo: str) -> str:
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    status_output = _git(
        repo,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    )
    _reject_unsupported_status(repo, status_output)
    staged = _git(
        repo,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
    )
    unstaged = _git(
        repo,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
    )
    untracked_output = _git(
        repo, "ls-files", "--others", "--exclude-standard", "-z"
    )
    if untracked_output and not untracked_output.endswith(b"\0"):
        raise UnsupportedState("Git returned a malformed untracked-file list")
    untracked_paths = (
        untracked_output[:-1].split(b"\0") if untracked_output else []
    )

    digest = hashlib.sha256()
    _frame(digest, b"head", head)
    _frame(digest, b"status", status_output)
    _frame(digest, b"staged", staged)
    _frame(digest, b"unstaged", unstaged)
    _frame(digest, b"untracked-list", untracked_output)

    repo_bytes = os.fsencode(os.path.abspath(repo))
    for relative_path in untracked_paths:
        full_path = os.path.join(repo_bytes, relative_path)
        try:
            file_stat = os.lstat(full_path)
        except FileNotFoundError as exc:
            raise UnsupportedState(
                "Git-visible untracked path disappeared while fingerprinting: "
                + os.fsdecode(relative_path)
            ) from exc

        _frame(digest, b"untracked-path", relative_path)
        _frame(
            digest,
            b"untracked-mode",
            stat.S_IMODE(file_stat.st_mode).to_bytes(4, "big"),
        )
        if stat.S_ISLNK(file_stat.st_mode):
            _frame(digest, b"untracked-type", b"symlink")
            _frame(digest, b"untracked-content", os.readlink(full_path))
        elif stat.S_ISREG(file_stat.st_mode):
            content_digest = hashlib.sha256()
            try:
                with open(full_path, "rb") as untracked_file:
                    for chunk in iter(lambda: untracked_file.read(1024 * 1024), b""):
                        content_digest.update(chunk)
            except (FileNotFoundError, IsADirectoryError) as exc:
                raise UnsupportedState(
                    "Git-visible untracked path changed type while fingerprinting: "
                    + os.fsdecode(relative_path)
                ) from exc
            _frame(digest, b"untracked-type", b"regular")
            _frame(digest, b"untracked-content", content_digest.digest())
        else:
            raise UnsupportedState(
                "COPILOT_READONLY=1 only supports untracked regular files "
                "and symlinks: "
                + os.fsdecode(relative_path)
            )

    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {os.path.basename(sys.argv[0])} REPOSITORY", file=sys.stderr)
        return 2
    try:
        print(fingerprint(sys.argv[1]))
    except UnsupportedState as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
