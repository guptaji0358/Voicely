"""Assemble a standalone Windows folder:   dist/Voicely/

    Voicely.exe                     tiny launcher (packaging/launcher.cpp): puts DLLs/ on the DLL search path, starts the app
    DLLs/                             the real app (Voicely-app.exe) + Qt/MinGW/ffmpeg DLLs + Qt plugins   (windeployqt)
    python/                           private CPython runtime (copied from the Python that runs this script) with ONLY the
                                      packages the worker needs (dependency closure computed from package metadata)
    engine/speech_engine/             the Python package (no tests, no caches)

Nothing is downloaded here, so the build is reproducible offline. Large optional pieces are kept OUT of the folder:
  * Kokoro model files (~310 MB)     -> downloaded on first run to %LOCALAPPDATA%\\speech_engine\\models (consent-gated)
  * speech-recognition model         -> downloaded from Settings > Pronunciation (consent-gated)
  * PyTorch / sounddevice / pytest / miniaudio ... are NOT copied (not needed at run time)

Options:
  --with-google    include google-genai (cloud TTS backend and the optional Gemini LLM provider)
  --with-asr       include faster-whisper and its dependencies IF they are installed in this Python (needed for Recheck Speech)
  --with-lab       include the optional Model Lab package (model_lab/, ~150 KB of Python; no datasets, no checkpoints)
  --with-torch     with --with-lab: also bundle PyTorch (CPU build, several hundred MB). Without it Model Lab asks for a
                   Python that has PyTorch (Settings) and the speech application is completely unaffected.
  --no-scipy       leave scipy out (~130 MB smaller; MP3 resampling falls back to linear interpolation)
  --verify         run packaging/verify_dist.py on the result

Usage:  python packaging/make_dist.py --build gui/build --qt D:/Apps/Qt6/6.11.1/mingw_64 --verify
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PKGS = ["kokoro-onnx", "lameenc", "numpy"]            # + closure
SKIP_PARTS = {"__pycache__", "tests", "test", "testing", "_pytest", "benchmarks", "doc", "docs"}
STDLIB_SKIP = {"test", "tests", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip", "pydoc_data", "__pycache__",
               "site-packages", "venv", "distutils"}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def closure(roots: list[str]) -> dict[str, md.Distribution]:
    seen: dict[str, md.Distribution] = {}
    todo = list(roots)
    while todo:
        n = todo.pop()
        if norm(n) in seen:
            continue
        try:
            d = md.distribution(n)
        except md.PackageNotFoundError:
            print(f"  ! required package not installed here: {n}", file=sys.stderr)
            continue
        seen[norm(n)] = d
        for r in d.requires or []:
            if "extra ==" in r:
                continue
            todo.append(re.match(r"[A-Za-z0-9_.\-]+", r).group(0))
    return seen


def copy_dist_files(d: md.Distribution, dest: Path) -> int:
    n = 0
    for f in d.files or []:
        parts = set(Path(str(f)).parts)
        if parts & SKIP_PARTS or str(f).endswith((".pyc", ".pyi")) and False:
            continue
        if str(f).startswith(("..", "Scripts", "bin")) or ".." in Path(str(f)).parts:
            continue
        src = Path(d.locate_file(f))
        if not src.is_file():
            continue
        out = dest / str(f)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        n += 1
    return n


def copy_runtime(dest: Path) -> None:
    base = Path(sys.base_prefix)
    dest.mkdir(parents=True)
    for name in ("python.exe", "pythonw.exe", "LICENSE.txt"):
        if (base / name).is_file():
            shutil.copy2(base / name, dest / name)
    for dll in base.glob("*.dll"):                               # python3xx.dll, vcruntime, ...
        shutil.copy2(dll, dest / dll.name)
    if (base / "DLLs").is_dir():
        shutil.copytree(base / "DLLs", dest / "DLLs", ignore=shutil.ignore_patterns("tcl*", "tk*", "_tkinter*", "*.lib", "__pycache__"))
    shutil.copytree(base / "Lib", dest / "Lib",
                    ignore=lambda d, names: [n for n in names if n in STDLIB_SKIP or n.endswith(".pyc")])
    (dest / "Lib" / "site-packages").mkdir(exist_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", default=str(ROOT / "gui" / "build"), help="CMake build folder containing Voicely.exe")
    ap.add_argument("--qt", default=os.environ.get("QT_ROOT", ""), help="Qt kit folder (contains bin/windeployqt.exe)")
    ap.add_argument("--out", default=str(ROOT / "dist" / "Voicely"))
    ap.add_argument("--with-google", action="store_true")
    ap.add_argument("--with-asr", action="store_true")
    ap.add_argument("--with-lab", action="store_true")
    ap.add_argument("--with-torch", action="store_true")
    ap.add_argument("--no-scipy", action="store_true")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()

    build, out = Path(a.build), Path(a.out)
    exe = build / "Voicely.exe"
    if not exe.is_file():
        sys.exit(f"{exe} not found - build the GUI first (cmake --build gui/build)")
    windeploy = Path(a.qt) / "bin" / "windeployqt.exe" if a.qt else Path(shutil.which("windeployqt") or "")
    if not windeploy.is_file():
        sys.exit("windeployqt not found: pass --qt <Qt kit folder> or put it on PATH")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    print("1/4 application + Qt (windeployqt) -> DLLs/, launcher -> Voicely.exe")
    libs = out / "DLLs"
    libs.mkdir()
    shutil.copy2(exe, libs / "Voicely-app.exe")
    env = dict(os.environ, PATH=str(windeploy.parent) + os.pathsep + os.environ["PATH"])
    subprocess.run([str(windeploy), "--no-translations", "--no-system-d3d-compiler", "--no-opengl-sw", "--compiler-runtime",
                    str(libs / "Voicely-app.exe")], check=True, env=env)
    gxx = Path(a.qt) / ".." / ".." / "Tools" / "mingw1310_64" / "bin" / "g++.exe"
    cxx = str(gxx.resolve()) if gxx.is_file() else shutil.which("g++")
    if not cxx:
        sys.exit("g++ not found (needed to build the launcher): put MinGW g++ on PATH")
    subprocess.run([cxx, "-O2", "-s", "-municode", "-mwindows", "-static", str(ROOT / "packaging" / "launcher.cpp"),
                    "-o", str(out / "Voicely.exe")], check=True)

    print("2/4 engine")
    shutil.copytree(ROOT / "speech_engine", out / "engine" / "speech_engine",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    if a.with_lab:
        try:
            shutil.copytree(ROOT / "model_lab", out / "engine" / "model_lab", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            print("    + model_lab (optional Model Lab package)")
        except Exception as exc:                      # an optional feature must never stop the base build
            print(f"    ! Model Lab packaging failed ({exc}); continuing without it", file=sys.stderr)
            a.with_lab = a.with_torch = False

    print("3/4 python runtime")
    py = out / "python"
    copy_runtime(py)
    roots = list(RUNTIME_PKGS) + ([] if a.no_scipy else ["scipy"]) + (["google-genai"] if a.with_google else []) \
        + (["faster-whisper"] if a.with_asr else []) + (["torch"] if (a.with_lab and a.with_torch) else [])
    pk = closure(roots)
    for k in ("torchvision", "sounddevice", "pytest", "miniaudio", "pyinstaller") + (() if (a.with_lab and a.with_torch) else ("torch",)):
        pk.pop(k, None)                                           # never part of the runtime unless explicitly requested
    site = py / "Lib" / "site-packages"
    total = 0
    for name, d in sorted(pk.items()):
        total += copy_dist_files(d, site)
    print(f"    {len(pk)} packages: {', '.join(sorted(pk))}")
    if a.with_asr and "faster-whisper" not in pk:
        print("    ! faster-whisper is not installed in this Python; Recheck Speech will need it (pip install faster-whisper)")

    print("4/4 notes")
    (out / "README-dist.txt").write_text(
        "Voicely (standalone folder)\n\n"
        "Run Voicely.exe. It uses ./python and ./engine automatically.\n"
        "First run: Kokoro model files are downloaded on request (about 310 MB, or ~80 MB int8) to %LOCALAPPDATA%\\speech_engine\\models.\n"
        "Recheck Speech needs the faster-whisper package in ./python and a recognition model (Settings > Pronunciation > Download).\n"
        "Nothing is uploaded; AI suggestions are off until you enable them.\n"
        + (("Model Lab (optional): engine/model_lab is included"
            + (" with PyTorch.\n" if a.with_torch else "; it needs a Python with PyTorch (Settings) - the speech app does not.\n"))
           if a.with_lab else "Model Lab is not included in this build.\n"), encoding="utf-8")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"done: {out}  ({size / 1e6:.0f} MB)")

    if a.verify:
        return subprocess.call([sys.executable, str(ROOT / "packaging" / "verify_dist.py"), str(out)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
