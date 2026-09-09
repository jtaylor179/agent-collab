#!/usr/bin/env python3
"""Guard against agent-collab version drift.

Checks that the version is identical across every place it's declared — the Claude,
Codex, and Cursor plugin manifests, the marketplace entries, and (if built) the
packaged dist/agent-collab.plugin. Exits non-zero with a clear report on any mismatch.

Run by sync.sh (before pushing to installs) and by the test suite. The real run that
shipped 0.2.9 left two manifests at 0.2.8 and the dist artifact stale — this catches
exactly that.
"""
import json
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))


def _plugin_version(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return json.load(f)["version"]


def _marketplace_version(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return json.load(f)["plugins"][0]["version"]


def collect():
    versions = {
        "plugins/agent-collab/.claude-plugin/plugin.json": _plugin_version(
            "plugins/agent-collab/.claude-plugin/plugin.json"),
        "plugins/agent-collab/.codex-plugin/plugin.json": _plugin_version(
            "plugins/agent-collab/.codex-plugin/plugin.json"),
        "plugins/agent-collab/.cursor-plugin/plugin.json": _plugin_version(
            "plugins/agent-collab/.cursor-plugin/plugin.json"),
        ".claude-plugin/marketplace.json": _marketplace_version(
            ".claude-plugin/marketplace.json"),
        ".cursor-plugin/marketplace.json": _marketplace_version(
            ".cursor-plugin/marketplace.json"),
    }
    dist = os.path.join(ROOT, "dist", "agent-collab.plugin")
    if os.path.exists(dist):
        with zipfile.ZipFile(dist) as z:
            versions["dist/agent-collab.plugin"] = json.loads(
                z.read(".claude-plugin/plugin.json"))["version"]
    return versions


REBUILD_HINT = (
    "  (cd plugins/agent-collab && zip -r /tmp/agent-collab.plugin . "
    "-x '*/__pycache__/*') && cp /tmp/agent-collab.plugin dist/agent-collab.plugin")


def _is_noise(rel):
    parts = rel.split(os.sep)
    return "__pycache__" in parts or parts[-1] in (".DS_Store",) or rel.endswith(".pyc")


def content_drift():
    """Compare every plugin source file against its copy inside dist/.

    Matching version strings are not enough: a bin/ change committed without a
    rebuild leaves dist/ serving stale code under an unchanged version, and
    sync.sh only forces a reinstall when the version string moves. That drift
    shipped silently before this check existed.
    """
    dist = os.path.join(ROOT, "dist", "agent-collab.plugin")
    if not os.path.exists(dist):
        return []
    plugin_dir = os.path.join(ROOT, "plugins", "agent-collab")
    with zipfile.ZipFile(dist) as z:
        packaged = {n: z.read(n) for n in z.namelist()
                    if not n.endswith("/") and not _is_noise(n)}
    problems = []
    on_disk = set()
    for dirpath, dirnames, filenames in os.walk(plugin_dir):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, plugin_dir)
            if _is_noise(rel):
                continue
            on_disk.add(rel)
            key = rel.replace(os.sep, "/")
            if key not in packaged:
                problems.append(f"missing from dist: {rel}")
                continue
            with open(full, "rb") as f:
                if f.read() != packaged[key]:
                    problems.append(f"stale in dist:    {rel}")
    for key in packaged:
        if key.replace("/", os.sep) not in on_disk:
            problems.append(f"extra in dist:    {key}")
    return sorted(problems)


def main():
    versions = collect()
    canonical = versions["plugins/agent-collab/.claude-plugin/plugin.json"]
    consistent = len(set(versions.values())) == 1
    print("agent-collab version check:")
    for path, v in versions.items():
        print(f"  [{'ok' if v == canonical else 'DRIFT'}] {v:<8} {path}")
    if not consistent:
        print(
            f"\nVERSION DRIFT detected: {sorted(set(versions.values()))}.\n"
            "Set the same version in all plugin and marketplace manifests and rebuild the package:\n"
            + REBUILD_HINT,
            file=sys.stderr)
        return 1
    print(f"\nAll consistent at {canonical}.")

    problems = content_drift()
    print("\nagent-collab dist content check:")
    if problems:
        for problem in problems:
            print(f"  [DRIFT] {problem}")
        print(
            f"\nCONTENT DRIFT detected in dist/agent-collab.plugin "
            f"({len(problems)} file(s)).\nThe version says {canonical} but the "
            "packaged files differ from source. Rebuild:\n" + REBUILD_HINT,
            file=sys.stderr)
        return 1
    print("  [ok] dist/agent-collab.plugin matches plugins/agent-collab/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
