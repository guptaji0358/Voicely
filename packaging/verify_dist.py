"""Smoke-test a dist/TTSStudio folder in a CLEAN environment (no system Python, no Qt on PATH, no PYTHONPATH):

  1. the bundled python.exe imports every runtime dependency and the worker module;
  2. the bundled worker answers health / analyze_text / a session synthesis (mock backend) / recheck (mock ASR) /
     dictionary / feedback-consent / llm_status over protocol 2;
  3. TTSStudio.exe starts (offscreen), finds the bundled python + engine by itself and renders a window snapshot.

Usage: python packaging/verify_dist.py dist/TTSStudio
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def clean_env(extra: dict | None = None) -> dict:
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env = {"SystemRoot": sysroot, "PATH": rf"{sysroot}\system32;{sysroot}", "TEMP": tempfile.gettempdir(),
           "TMP": tempfile.gettempdir(), "PYTHONIOENCODING": "utf-8", "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", "")}
    env.update(extra or {})
    return env


def main(dist: Path) -> int:
    py, engine, exe = dist / "python" / "python.exe", dist / "engine", dist / "TTSStudio.exe"
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= bool(cond)
        print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))

    check("files present", py.is_file() and (engine / "speech_engine" / "worker.py").is_file() and exe.is_file())
    work = Path(tempfile.mkdtemp(prefix="tts_dist_"))
    data = work / "data"
    data.mkdir()

    r = subprocess.run([str(py), "-I", "-c",
                        "import numpy, onnxruntime, kokoro_onnx, lameenc, phonemizer, espeakng_loader, sys;"
                        "print('ok', sys.prefix)"], capture_output=True, text=True, env=clean_env(), cwd=work)
    check("bundled python imports runtime dependencies", r.returncode == 0 and "ok" in r.stdout, r.stderr[-300:])
    has_lab = (engine / "model_lab" / "worker.py").is_file()
    has_torch = (dist / "python" / "Lib" / "site-packages" / "torch").is_dir()
    r = subprocess.run([str(py), "-I", "-c", "import torch"], capture_output=True, text=True, env=clean_env(), cwd=work)
    check("PyTorch is bundled only when Model Lab was built with --with-torch", (r.returncode == 0) == (has_lab and has_torch),
          r.stderr[-200:])
    if has_lab:                       # optional Model Lab: its worker must start and describe its own state honestly
        lab_root = work / "lab"
        lab_env = clean_env({"PYTHONPATH": str(engine), "MODEL_LAB_DIR": str(lab_root), "SPEECH_ENGINE_DATA_DIR": str(data)})
        lp = subprocess.Popen([str(py), "-X", "utf8", "-m", "model_lab.worker"], cwd=work, env=lab_env, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ready = json.loads(lp.stdout.readline())
        lp.stdin.write(json.dumps(dict(protocol_version=2, request_id="h", action="health")).encode() + b"\n")
        lp.stdin.flush()
        h = json.loads(lp.stdout.readline())
        lp.stdin.close()
        lp.wait(20)
        check("Model Lab worker starts from the bundled runtime", ready.get("worker") == "model_lab" and h.get("status") == "success", str(h)[:300])
        check("Model Lab reports PyTorch availability truthfully", h.get("torch_available") == has_torch,
              f"torch_available={h.get('torch_available')} bundled={has_torch}")
    r = subprocess.run([str(py), "-c", "import sys; print(sys.prefix)"], capture_output=True, text=True, env=clean_env(), cwd=work)
    check("python runs from inside the dist folder (not the system install)", str(dist).lower() in r.stdout.lower(), r.stdout)

    asr = work / "asr.json"
    asr.write_text(json.dumps({"0000.g0.wav": "hello world", "0001.g0.wav": "please call win now"}), encoding="utf-8")
    env = clean_env({"PYTHONPATH": str(engine), "SPEECH_ENGINE_ALLOW_MOCK": "1", "SPEECH_ENGINE_ASR": "mock",
                     "SPEECH_ENGINE_MOCK_ASR_FILE": str(asr), "SPEECH_ENGINE_DATA_DIR": str(data)})
    p = subprocess.Popen([str(py), "-X", "utf8", "-m", "speech_engine.worker"], cwd=work, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def read():
        return json.loads(p.stdout.readline())

    def call(action, **kw):
        call.n += 1
        rid = f"r{call.n}"
        p.stdin.write(json.dumps(dict(protocol_version=2, request_id=rid, action=action, **kw)).encode() + b"\n")
        p.stdin.flush()
        while True:
            m = read()
            if m.get("request_id") == rid and "status" in m:
                return m
    call.n = 0
    check("worker ready", read().get("event") == "ready")
    h = call("health")
    check("health v2 reports asr/llm/dictionary", h["status"] == "success" and "asr" in h and "llm" in h and "dictionary" in h)
    check("mp3 encoder bundled", h["mp3"]["available"], str(h["mp3"]))
    a = call("analyze_text", text="Please call Nguyen about the API.", backend="mock")
    check("analyze_text", a["status"] == "success" and a["counts"]["total"] > 0)
    s = call("synthesize", text="Hello world.\n\nPlease call Nguyen now.", backend="mock", voice="original",
             output_path=str(work / "o.wav"), output_format="wav", overwrite=True, session_dir=str(work / "sess"))
    check("session synthesis", s["status"] == "success" and (work / "sess" / "manifest.json").is_file(), json.dumps(s)[:200])
    rc = call("recheck", session_dir=str(work / "sess"), thresholds={"min_rate": 0, "max_rate": 1e9})
    check("recheck (mock ASR)", rc["status"] == "success" and rc["summary"]["status"] == "needs_review", json.dumps(rc)[:200])
    m = call("export", input_path=str(work / "o.wav"), output_path=str(work / "o.mp3"), output_format="mp3", overwrite=True)
    check("mp3 export", m["status"] == "success" and (work / "o.mp3").stat().st_size > 1000)
    model_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "speech_engine" / "models"
    if (model_dir / "voices-v1.0.bin").is_file() and any(model_dir.glob("kokoro-v1.0*.onnx")):
        # REAL (non-mock) speech from the bundled runtime, including an IPA override
        real = call("synthesize", text="Please call Nguyen now.", backend="local", voice="original",
                    output_path=str(work / "real.wav"), output_format="wav", overwrite=True, session_dir=str(work / "sess_real"))
        check("real local synthesis from the bundled runtime", real["status"] == "success" and real["duration_s"] > 0.5, json.dumps(real)[:200])
    else:
        print("SKIP  real local synthesis (Kokoro model not installed)")
    check("dictionary add", call("dictionary", op="add", entry={"written": "Nguyen", "hint": "win"})["added"])
    check("llm disabled by default", call("llm_status")["enabled"] is False)
    check("feedback needs consent", call("feedback", op="record", record={"original_text": "x", "term": "x",
          "correction": {"hint": "y"}})["code"] == "CONSENT_REQUIRED")
    p.stdin.close()
    p.wait(15)
    check("worker exits cleanly", p.returncode == 0, str(p.returncode))

    shot = work / "shot.png"
    # real "windows" platform plugin (the one that ships): the window is visible for a few seconds, then the app exits itself
    env = clean_env({"TTS_SNAPSHOT": str(shot), "SPEECH_ENGINE_DATA_DIR": str(data), "SPEECH_ENGINE_ALLOW_MOCK": "1"})
    r = subprocess.run([str(exe)], cwd=work, env=env, capture_output=True, text=True, timeout=120)
    check("TTSStudio.exe starts without Qt/Python on PATH and renders", r.returncode == 0 and shot.is_file() and shot.stat().st_size > 5000,
          (r.stderr or "")[-300:])
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "dist/TTSStudio").resolve()))
