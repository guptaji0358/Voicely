"""Rebuild dist/Voicely when the sources changed since the last build (used by the Claude Code Stop hook).

    python packaging/update_binary.py [--force]

Hashes speech_engine/, gui/src, gui/CMakeLists.txt and packaging/ (not caches, tests or generated samples) and compares
it with dist/.build_stamp. If different: cmake --build gui/build, then packaging/make_dist.py --verify. The stamp is
only written after a verified build, so a failed build is retried next time. Prints a one-line JSON status for the hook.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist" / "Voicely"
STAMP = ROOT / "dist" / ".build_stamp"
QT = os.environ.get("QT_ROOT", "D:/Apps/Qt6/6.11.1/mingw_64")
TOOLS = ["D:/Apps/Qt6/Tools/mingw1310_64/bin", "D:/Apps/Qt6/Tools/Ninja"]
WATCH = ["speech_engine", "gui/src", "gui/CMakeLists.txt", "packaging"]
SKIP = {"__pycache__", "tests", "voice_samples", "build"}
SKIP_FILES = {"update_binary.py"}


def source_hash() -> str:
    h = hashlib.sha256()
    for rel in WATCH:
        p = ROOT / rel
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
        for f in files:
            if SKIP & set(f.relative_to(ROOT).parts) or f.suffix in (".pyc", ".wav") or f.name in SKIP_FILES:
                continue
            h.update(f.relative_to(ROOT).as_posix().encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def say(msg: str, ok: bool = True) -> int:
    print(json.dumps({"systemMessage": msg}))
    return 0 if ok else 1


def run(cmd, **kw) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=os.pathsep.join(TOOLS) + os.pathsep + os.environ["PATH"])
    return subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def clear_dist() -> str:
    """rmtree can fail on OneDrive-synced folders; PowerShell's Remove-Item copes, so retry with it."""
    for _ in range(3):
        shutil.rmtree(DIST, ignore_errors=True)
        if not DIST.exists():
            return ""
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Remove-Item -Recurse -Force '{DIST}'"],
                       capture_output=True)
        if not DIST.exists():
            return ""
    return f"{DIST} still exists"


def main() -> int:
    digest = source_hash()
    if "--force" not in sys.argv and STAMP.is_file() and DIST.is_dir() and STAMP.read_text().strip() == digest:
        return say("Binary is up to date (dist/Voicely)")
    r = run(["cmake", "--build", str(ROOT / "gui" / "build")])
    if r.returncode:
        return say("Binary NOT updated: GUI build failed\n" + (r.stdout + r.stderr)[-1500:], False)
    if DIST.exists():
        err = clear_dist()
        if err:
            return say(f"Binary NOT updated: cannot clear dist/Voicely (is Voicely running?): {err}", False)
    r = run([sys.executable, str(ROOT / "packaging" / "make_dist.py"), "--build", str(ROOT / "gui" / "build"),
             "--qt", QT, "--with-google", "--verify"])
    if r.returncode or "ALL CHECKS PASSED" not in r.stdout:
        return say("Binary NOT updated: packaging/verification failed\n" + (r.stdout + r.stderr)[-1500:], False)
    STAMP.write_text(digest)
    return say("Binary updated: dist/Voicely rebuilt and verified")


if __name__ == "__main__":
    sys.exit(main())
