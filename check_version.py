#!/usr/bin/env python3
"""Guard against agent-collab version drift.

Checks that the version is identical across every place it's declared — the Claude
plugin manifest, the Codex plugin manifest, the marketplace entry, and (if built) the
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
        ".claude-plugin/marketplace.json": _marketplace_version(
            ".claude-plugin/marketplace.json"),
    }
    dist = os.path.join(ROOT, "dist", "agent-collab.plugin")
    if os.path.exists(dist):
        with zipfile.ZipFile(dist) as z:
            versions["dist/agent-collab.plugin"] = json.loads(
                z.read(".claude-plugin/plugin.json"))["version"]
    return versions


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
            "Set the same version in all three manifests and rebuild the package:\n"
            "  (cd plugins/agent-collab && zip -r /tmp/agent-collab.plugin . "
            "-x '*/__pycache__/*') && cp /tmp/agent-collab.plugin dist/agent-collab.plugin",
            file=sys.stderr)
        return 1
    print(f"\nAll consistent at {canonical}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
