#!/usr/bin/env python3
"""Build dist/agent-collab.plugin reproducibly on any supported platform."""
from pathlib import Path
import stat
import zipfile


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "plugins" / "agent-collab"
OUTPUT = ROOT / "dist" / "agent-collab.plugin"


def included_files():
    for path in sorted(SOURCE.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(SOURCE)
        if "__pycache__" in relative.parts or path.suffix == ".pyc" \
                or path.name == ".DS_Store":
            continue
        yield path, relative.as_posix()


def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUTPUT.with_suffix(".plugin.tmp")
    with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=9) as archive:
        for path, name in included_files():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # Preserve runnable launchers even when the archive is built on Windows,
            # whose filesystem mode bits do not carry POSIX executability.
            mode = 0o755 if path.suffix in {".py", ".sh"} else 0o644
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    temporary.replace(OUTPUT)
    print(f"built {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
