#!/usr/bin/env python3
"""Run GitHub Copilot CLI under ``collab watch --exec``.

The watcher supplies a claimed message on stdin while Copilot expects its prompt
as ``-p/--prompt``.  This is the native, cross-platform counterpart of
``copilot-exec.sh``.  It is also the Windows read-only implementation: Copilot
runs in a verified disposable Git clone with no remote, object alternates, live
checkout arguments, or inherited environment values that name the source Git
state.  macOS additionally uses ``sandbox-exec`` to deny source-worktree writes.

``COPILOT_READONLY=0`` deliberately disables snapshot isolation and gives
Copilot the caller's live repository.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


DEFAULT_MODEL = "claude-opus-4.8"
DEFAULT_REASONING_EFFORT = "high"


class AdapterError(RuntimeError):
    def __init__(self, message, status=1):
        super().__init__(message)
        self.status = status


def _resolve_copilot():
    override = os.environ.get("COPILOT_BIN")
    if override:
        return shutil.which(override) or (
            override if Path(override).is_file() else None
        )
    return shutil.which("copilot")


def _git(repo, *args, input_bytes=None):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        command = "git " + " ".join(args)
        raise AdapterError(
            command + " failed" + (f": {detail}" if detail else ""),
            completed.returncode,
        )
    return completed.stdout


def _git_text(repo, *args):
    return os.fsdecode(_git(repo, *args).rstrip(b"\r\n"))


def _fingerprint(repo, script_dir):
    completed = subprocess.run(
        [sys.executable, str(script_dir / "copilot-snapshot-state.py"), str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise AdapterError(
            detail or "could not fingerprint the Copilot snapshot",
            completed.returncode,
        )
    return completed.stdout.strip()


def _is_within(path, root):
    try:
        os.path.commonpath((os.path.abspath(path), os.path.abspath(root)))
    except ValueError:
        return False
    return os.path.normcase(os.path.commonpath((
        os.path.abspath(path), os.path.abspath(root)
    ))) == os.path.normcase(os.path.abspath(root))


def _protected_paths(source_repo, source_git_dir, source_git_common):
    protected = {
        os.path.abspath(str(source_repo)),
        os.path.abspath(str(source_git_dir)),
        os.path.abspath(str(source_git_common)),
    }
    # A linked worktree's common Git dir names the other live checkout as
    # ``<main-worktree>/.git``.  Protect that worktree root too.
    if Path(source_git_common).name.lower() == ".git":
        protected.add(os.path.abspath(str(Path(source_git_common).parent)))
    return sorted(protected, key=len, reverse=True)


def _normal_for_match(value):
    value = os.path.normcase(value).replace("/", os.sep).replace("\\", os.sep)
    return value.rstrip(os.sep)


def _references_protected_path(value, protected):
    if not value:
        return False
    normalized = _normal_for_match(value)
    return any(_normal_for_match(path) in normalized for path in protected)


def _sanitized_environment(protected, snapshot_repo):
    """Remove every ordinary route from the Copilot child back to live Git state."""
    blocked_names = {
        "PWD", "OLDPWD", "INIT_CWD", "CLAUDE_PROJECT_DIR",
        "NPM_CONFIG_LOCAL_PREFIX", "NPM_PACKAGE_JSON",
    }
    clean = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if upper in blocked_names or upper.startswith(("COLLAB_", "GIT_")):
            continue
        if upper == "PATH" and _references_protected_path(value, protected):
            entries = [
                entry for entry in value.split(os.pathsep)
                if not _references_protected_path(entry, protected)
            ]
            clean[name] = os.pathsep.join(entries)
            continue
        if _references_protected_path(value, protected):
            continue
        clean[name] = value
    clean["PWD"] = str(snapshot_repo)
    return clean


def _copy_untracked(source_repo, snapshot_repo, untracked_output):
    if untracked_output and not untracked_output.endswith(b"\0"):
        raise AdapterError("Git returned a malformed untracked-file list", 2)
    records = untracked_output[:-1].split(b"\0") if untracked_output else []
    for raw_path in records:
        relative = Path(os.fsdecode(raw_path))
        if relative.is_absolute() or ".." in relative.parts:
            raise AdapterError(
                f"unsafe Git-visible untracked path: {relative}", 2
            )
        source = source_repo / relative
        destination = snapshot_repo / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            target = os.readlink(source)
            os.symlink(target, destination, target_is_directory=source.is_dir())
        elif source.is_file():
            shutil.copy2(source, destination)
        else:
            raise AdapterError(
                "Git-visible untracked path disappeared while snapshotting: "
                + str(relative)
            )


def _reject_external_links(snapshot_repo):
    """A clone-only Windows boundary cannot safely expose an escaping link."""
    for root, directories, files in os.walk(snapshot_repo, followlinks=False):
        for name in list(directories) + files:
            candidate = Path(root) / name
            is_junction = getattr(candidate, "is_junction", lambda: False)()
            if not candidate.is_symlink() and not is_junction:
                continue
            if name in directories:
                directories.remove(name)
            resolved = os.path.realpath(candidate)
            if not _is_within(resolved, snapshot_repo):
                raise AdapterError(
                    "COPILOT_READONLY=1 cannot isolate a repository containing "
                    f"an external symlink or junction: {candidate}",
                    2,
                )


def _parse_readonly_args(args):
    source_dir = os.getcwd()
    forwarded = []
    expect_source = False
    for arg in args:
        if expect_source:
            source_dir = arg
            expect_source = False
            continue
        if arg == "-C":
            expect_source = True
        elif arg.startswith("-C") and len(arg) > 2:
            source_dir = arg[2:]
        elif arg == "--add-dir" or arg.startswith("--add-dir="):
            raise AdapterError(
                "COPILOT_READONLY=1 does not permit --add-dir; review one "
                "isolated repository",
                2,
            )
        else:
            forwarded.append(arg)
    if expect_source:
        raise AdapterError("-C requires a repository path", 2)
    return Path(source_dir), forwarded


def _sandbox_prefix(source_repo, source_git_dir, source_git_common):
    if sys.platform != "darwin":
        return []
    if shutil.which("sandbox-exec") is None:
        raise AdapterError(
            "COPILOT_READONLY=1 requires sandbox-exec on macOS", 2
        )
    profile = """(version 1)
(allow default)
(deny file-write* (subpath (param \"SOURCE_REPO\")))
(deny file-write* (subpath (param \"SOURCE_GIT_DIR\")))
(deny file-write* (subpath (param \"SOURCE_GIT_COMMON\")))"""
    return [
        "sandbox-exec",
        "-D", f"SOURCE_REPO={source_repo}",
        "-D", f"SOURCE_GIT_DIR={source_git_dir}",
        "-D", f"SOURCE_GIT_COMMON={source_git_common}",
        "-p", profile,
    ]


def _build_snapshot(source_dir, snapshot_repo, script_dir):
    try:
        source_repo = Path(_git_text(source_dir, "rev-parse", "--show-toplevel"))
    except AdapterError as exc:
        raise AdapterError(
            "COPILOT_READONLY=1 requires -C (or the current directory) to be "
            "inside a Git repository",
            2,
        ) from exc
    try:
        source_head = _git_text(source_repo, "rev-parse", "--verify", "HEAD")
    except AdapterError as exc:
        raise AdapterError(
            "COPILOT_READONLY=1 requires a repository with an existing HEAD commit",
            2,
        ) from exc

    source_git_dir = Path(_git_text(
        source_repo, "rev-parse", "--path-format=absolute", "--absolute-git-dir"
    ))
    source_git_common = Path(_git_text(
        source_repo, "rev-parse", "--path-format=absolute", "--git-common-dir"
    ))
    protected = _protected_paths(
        source_repo, source_git_dir, source_git_common
    )
    if any(_is_within(snapshot_repo, path) for path in protected):
        raise AdapterError(
            "COPILOT_READONLY=1 requires the system temporary directory to be "
            "outside every live worktree and Git metadata directory",
            2,
        )

    state_before = _fingerprint(source_repo, script_dir)
    staged_patch = _git(
        source_repo, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff"
    )
    unstaged_patch = _git(
        source_repo, "diff", "--binary", "--full-index", "--no-ext-diff"
    )
    untracked = _git(
        source_repo, "ls-files", "--others", "--exclude-standard", "-z"
    )

    clone = subprocess.run(
        [
            "git", "clone", "--quiet", "--no-local", "--no-hardlinks",
            "--no-checkout", str(source_repo), str(snapshot_repo),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if clone.returncode:
        detail = clone.stderr.decode(errors="replace").strip()
        raise AdapterError(
            "git clone failed" + (f": {detail}" if detail else ""),
            clone.returncode,
        )
    _git(snapshot_repo, "checkout", "--quiet", "--detach", source_head)
    _git(snapshot_repo, "remote", "remove", "origin")
    if staged_patch:
        _git(
            snapshot_repo, "apply", "--index", "--whitespace=nowarn",
            input_bytes=staged_patch,
        )
    if unstaged_patch:
        _git(
            snapshot_repo, "apply", "--whitespace=nowarn",
            input_bytes=unstaged_patch,
        )
    _copy_untracked(source_repo, snapshot_repo, untracked)
    _reject_external_links(snapshot_repo)

    snapshot_state = _fingerprint(snapshot_repo, script_dir)
    source_state_after = _fingerprint(source_repo, script_dir)
    if state_before != source_state_after or state_before != snapshot_state:
        raise AdapterError(
            "source repository changed while creating the read-only snapshot; "
            "review not started"
        )
    alternates = snapshot_repo / ".git" / "objects" / "info" / "alternates"
    if alternates.exists() or _git(snapshot_repo, "remote", "-v"):
        raise AdapterError(
            "read-only snapshot retained a path back to the source repository"
        )
    return source_repo, source_git_dir, source_git_common, protected


def _custom_instruction_args():
    setting = os.environ.get("COPILOT_CUSTOM_INSTRUCTIONS", "1").lower()
    if setting in ("1", "true", "on"):
        return []
    if setting in ("0", "false", "off"):
        return ["--no-custom-instructions"]
    raise AdapterError(
        "COPILOT_CUSTOM_INSTRUCTIONS must be 0/1, false/true, or off/on", 2
    )


def _run_copilot(binary, forwarded, prompt, process_dir, child_env, sandbox):
    readonly_args = (
        ["--deny-tool", "write", "edit", "create", "apply_patch"]
        if os.environ.get("COPILOT_READONLY", "1") != "0" else []
    )
    command = [
        *sandbox,
        binary,
        "--allow-all-tools",
        *readonly_args,
        *_custom_instruction_args(),
        "--model", os.environ.get("COPILOT_MODEL", DEFAULT_MODEL),
        "--reasoning-effort", os.environ.get(
            "COPILOT_REASONING_EFFORT", DEFAULT_REASONING_EFFORT
        ),
        *forwarded,
        "--silent", "--output-format", "json", "--stream", "off",
        "-p", prompt,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(process_dir),
            env=child_env,
            stdout=subprocess.PIPE,
        )
    except OSError as exc:
        raise AdapterError(f"failed to run Copilot CLI: {exc}") from exc
    if completed.returncode:
        raise AdapterError(
            f"copilot exited with status {completed.returncode}; response withheld",
            completed.returncode,
        )
    extractor = Path(__file__).resolve().parent / "copilot-jsonl-extract.py"
    extracted = subprocess.run([sys.executable, str(extractor)], input=completed.stdout)
    return extracted.returncode


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        prompt = sys.stdin.buffer.read().decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        print(f"copilot-exec: stdin is not valid UTF-8: {exc}", file=sys.stderr)
        return 2
    binary = _resolve_copilot()
    if binary is None:
        print(
            "copilot-exec: Copilot CLI not found (install copilot or set COPILOT_BIN)",
            file=sys.stderr,
        )
        return 1

    try:
        if os.environ.get("COPILOT_READONLY", "1") == "0":
            return _run_copilot(
                binary, argv, prompt, Path.cwd(), os.environ.copy(), []
            )

        if os.name != "nt" and sys.platform != "darwin":
            raise AdapterError(
                "COPILOT_READONLY=1 has no enforced write sandbox for this platform",
                2,
            )
        source_dir, forwarded = _parse_readonly_args(argv)
        script_dir = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="agent-collab-copilot.") as temp:
            snapshot_repo = Path(temp) / "repo"
            source_repo, git_dir, git_common, protected = _build_snapshot(
                source_dir, snapshot_repo, script_dir
            )
            for arg in forwarded:
                if _references_protected_path(arg, protected):
                    raise AdapterError(
                        "COPILOT_READONLY=1 cannot forward an argument naming a "
                        "live worktree",
                        2,
                    )
            sandbox = _sandbox_prefix(source_repo, git_dir, git_common)
            child_env = _sanitized_environment(protected, snapshot_repo)
            return _run_copilot(
                binary,
                [*forwarded, "-C", str(snapshot_repo)],
                prompt,
                snapshot_repo,
                child_env,
                sandbox,
            )
    except AdapterError as exc:
        print(str(exc), file=sys.stderr)
        return exc.status


if __name__ == "__main__":
    raise SystemExit(main())
