"""JSON-lines worker for the Qt GUI (and any other front end).

    python -m speech_engine.worker

Protocol v1 (docs/PROTOCOL.md): one UTF-8 JSON object per line.
  * stdin  : requests   {"protocol_version":1,"request_id":..,"action":..,...}
  * stdout : ONLY protocol messages (events and final responses). Nothing else is ever written there.
  * stderr : logs and tracebacks.
Audio bytes never travel over the protocol - only file paths and metadata.
One job (synthesize/export) runs at a time; a second one is rejected with BUSY.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
import wave
from pathlib import Path

PROTOCOL_VERSION = 2                      # current; version-1 requests are still accepted (see docs/PROTOCOL.md)
SUPPORTED_VERSIONS = (1, 2)
V1_ACTIONS = {"health", "capabilities", "cancel", "synthesize", "export", "shutdown"}
JOB_ACTIONS = {"synthesize", "export", "recheck", "regenerate", "llm_analyze", "llm_explain", "asr_download"}
MAX_TEXT_CHARS = 2_000_000
LANGUAGES = {  # Kokoro language codes; English-blend voices read other languages with an accent
    "en-us": "English (US)", "en-gb": "English (UK)", "fr-fr": "French", "es": "Spanish",
    "hi": "Hindi", "it": "Italian", "pt-br": "Portuguese (BR)", "ja": "Japanese", "cmn": "Mandarin",
}


class _Out:
    """Exclusive owner of the real stdout. Everything else is redirected to stderr."""

    def __init__(self):
        self._fh = os.fdopen(os.dup(1), "wb", buffering=0)
        os.dup2(2, 1)                      # native libraries writing to fd 1 now land in stderr
        sys.stdout = sys.stderr            # stray print() goes to stderr too
        self._lock = threading.Lock()

    def send(self, obj: dict) -> None:
        obj.setdefault("protocol_version", PROTOCOL_VERSION)
        line = json.dumps(obj, ensure_ascii=True, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._fh.write(line.encode("ascii"))
            except (BrokenPipeError, OSError):
                os._exit(0)                # GUI went away


SYNC_ACTIONS = {"analyze_text", "dictionary", "feedback", "llm_status", "llm_configure", "asr_status",
                "session_info", "segment", "accept_repair", "discard_repair"}


def log(msg: str) -> None:
    print(f"[worker] {msg}", file=sys.stderr, flush=True)


from .worker_defaults import DEFAULT_BACKEND, LOCAL_ENABLED, stretch_of as _stretch
from .worker_qa import QAMixin


class Worker(QAMixin):
    def __init__(self, out: _Out):
        self.out = out
        self.job: tuple[str, threading.Event] | None = None
        self.lock = threading.Lock()
        self.versions: dict[str, int] = {}     # request_id -> protocol version to answer with
        self._qa = None

    @property
    def qa(self):
        if self._qa is None:
            from .qa import QA
            self._qa = QA()
        return self._qa

    def _v(self, rid) -> int:
        return self.versions.get(rid, PROTOCOL_VERSION)

    # ---- helpers ---------------------------------------------------------
    def error(self, rid, code, message, **extra):
        self.out.send({"request_id": rid, "status": "error", "code": code, "message": message,
                       "protocol_version": self._v(rid), **extra})

    def ok(self, rid, **fields):
        self.out.send({"request_id": rid, "status": "success", "protocol_version": self._v(rid), **fields})

    def progress(self, rid, pct, message=""):
        self.out.send({"request_id": rid, "event": "progress", "progress": int(max(0, min(100, pct))),
                       "message": message, "protocol_version": self._v(rid)})

    # ---- dispatch --------------------------------------------------------
    def handle(self, line: str) -> None:
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            self.error(None, "MALFORMED_REQUEST", f"invalid JSON: {exc}")
            return
        rid = req.get("request_id")
        if not isinstance(rid, str) or not rid:
            self.error(None, "MALFORMED_REQUEST", "missing request_id")
            return
        ver = req.get("protocol_version")
        if ver not in SUPPORTED_VERSIONS or isinstance(ver, bool):
            self.error(rid, "UNSUPPORTED_PROTOCOL", f"worker speaks protocol {' and '.join(map(str, SUPPORTED_VERSIONS))}")
            return
        self.versions[rid] = ver
        if len(self.versions) > 2000:
            self.versions.pop(next(iter(self.versions)))
        action = req.get("action")
        if ver == 1 and action in (JOB_ACTIONS | SYNC_ACTIONS) - V1_ACTIONS:
            self.error(rid, "UNSUPPORTED_PROTOCOL", f"action {action!r} needs protocol_version 2")
            return
        try:
            if action == "health":
                self.health(rid, req)
            elif action == "capabilities":
                self.capabilities(rid)
            elif action == "cancel":
                self.cancel(rid, req)
            elif action in JOB_ACTIONS:
                self.start_job(rid, action, req)
            elif action in SYNC_ACTIONS:
                self.sync_action(rid, action, req)
            elif action == "shutdown":
                self.ok(rid)
                os._exit(0)
            else:
                self.error(rid, "UNKNOWN_ACTION", f"unknown action {action!r}")
        except Exception as exc:           # never let a bug kill the worker silently
            log(traceback.format_exc())
            self.error(rid, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")

    # ---- actions ---------------------------------------------------------
    def health(self, rid, req):
        from .encoders import encoder_status
        from .engine import make_backend
        names = (["local"] if LOCAL_ENABLED else []) + ["google"] + (["mock"] if os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1" else [])
        backends = {}
        for n in names:
            try:
                st = make_backend(n).status()
                backends[n] = {"available": st.available, "network": st.network,
                               "details": st.details, "problems": st.problems}
            except Exception as exc:
                backends[n] = {"available": False, "network": False, "details": [],
                               "problems": [f"{type(exc).__name__}: {exc}"]}
        enc = encoder_status()
        sel = req.get("backend")
        ready = (backends.get(sel, {}).get("available", False) if sel
                 else any(b["available"] for b in backends.values()))
        extra = {}
        if self._v(rid) >= 2:
            try:
                a = self.qa.make_asr(None).status()
                extra["asr"] = {"provider": a.provider, "available": a.available, "model": a.model, "problems": a.problems}
            except Exception as exc:
                extra["asr"] = {"provider": "?", "available": False, "model": "", "problems": [str(exc)]}
            try:
                extra["llm"] = self.qa.llm.status()
                extra["dictionary"] = {"version": self.qa.dictionary.version, "entries": len(self.qa.dictionary.entries)}
                extra["feedback"] = {"consent": self.qa.feedback.consent(), **self.qa.feedback.stats()}
            except Exception as exc:
                extra["llm"] = {"enabled": False, "available": False, "problems": [str(exc)]}
        self.ok(rid, python=sys.version.split()[0], executable=sys.executable, ready=ready, backends=backends,
                mp3={"available": enc["mp3"], "encoder": enc["encoder"], "problem": enc["problem"]}, **extra)

    def capabilities(self, rid):
        from .voice_profiles import load_profiles
        profiles = load_profiles()
        samples = Path(__file__).resolve().parent / "voice_samples"
        voices = [{"name": n, "description": p.description, "speed": p.speed,
                   "gender": p.gender, "display_name": p.display_name or n, "tagline": p.tagline,
                   "sample_path": next((str(samples / f"{n}{e}") for e in (".mp3", ".wav") if (samples / f"{n}{e}").is_file()), ""),
                   "kind": "prompt/config-defined profile (not a trained model)"}
                  for n, p in sorted(profiles.items())]
        backends = {"google": {"languages": {}, "speed": True, "style": True, "network": True}}
        if LOCAL_ENABLED:
            backends["local"] = {"languages": LANGUAGES, "speed": True, "style": False, "network": False}
        if os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1":
            backends["mock"] = {"languages": {"en-us": "tone"}, "speed": False, "style": False, "network": False}
        pron = {"local": {"respelling": True, "ipa": True}, "google": {"respelling": True, "ipa": False}}
        if os.environ.get("SPEECH_ENGINE_ALLOW_MOCK") == "1":
            pron["mock"] = {"respelling": True, "ipa": False}
        from .emotions import emotion_list
        self.ok(rid, voices=voices, emotions=emotion_list(), backends=backends, formats=["wav", "mp3"],
                bitrates=[64, 96, 128, 160, 192, 224, 256, 320],
                **({"pronunciation": pron, "protocol_versions": list(SUPPORTED_VERSIONS)} if self._v(rid) >= 2 else {}))

    def cancel(self, rid, req):
        target = req.get("target")
        with self.lock:
            if self.job and self.job[0] == target:
                self.job[1].set()
                self.ok(rid, cancelling=True)
                return
        self.error(rid, "NOT_RUNNING", f"no running job {target!r}")

    def start_job(self, rid, action, req):
        with self.lock:
            if self.job:
                self.error(rid, "BUSY", "another job is already running")
                return
            ev = threading.Event()
            self.job = (rid, ev)
        threading.Thread(target=self._run_job, args=(rid, action, req, ev), daemon=True).start()

    def _run_job(self, rid, action, req, cancel):
        from .exceptions import (BackendUnavailableError, ConfigurationError, EncoderUnavailableError,
                                 InvalidInputError, RateLimitError, SpeechEngineError, SynthesisCancelled)
        from .feedback import ConsentRequired
        from .llm.base import LLMError
        try:
            fn = {"synthesize": self.synthesize, "export": self.export, "recheck": self.recheck,
                  "regenerate": self.regenerate, "llm_analyze": self.llm_analyze, "llm_explain": self.llm_explain,
                  "asr_download": self.asr_download}[action]
            self.ok(rid, **fn(rid, req, cancel))
        except SynthesisCancelled:
            self.out.send({"request_id": rid, "status": "cancelled", "protocol_version": self._v(rid)})
        except LLMError as exc:
            log(f"{exc.code}: {exc}")
            self.error(rid, exc.code, str(exc))
        except ConsentRequired as exc:
            self.error(rid, "CONSENT_REQUIRED", str(exc))
        except SpeechEngineError as exc:
            code = {InvalidInputError: "INVALID_INPUT", ConfigurationError: "CONFIGURATION",
                    BackendUnavailableError: "BACKEND_UNAVAILABLE", RateLimitError: "RATE_LIMIT",
                    EncoderUnavailableError: "ENCODER_UNAVAILABLE"}.get(type(exc), "SYNTHESIS_FAILED")
            log(f"{code}: {exc}")
            self.error(rid, code, str(exc))
        except Exception as exc:
            log(traceback.format_exc())
            self.error(rid, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        finally:
            with self.lock:
                self.job = None
            # Light on RAM: the speech model (~400 MB) is loaded for the job, the audio goes to disk as it is produced,
            # and everything is released as soon as the job ends (measured: back to ~80 MB between jobs).
            import gc
            gc.collect()
            _trim_working_set()

    @staticmethod
    def _target(req, key="output_path"):
        from .encoders import resolve_format
        from .exceptions import InvalidInputError
        path = req.get(key)
        if not isinstance(path, str) or not path:
            raise InvalidInputError(f"{key} is required")
        fmt = resolve_format(path, req.get("output_format"))
        if fmt == "pcm":
            raise InvalidInputError("raw PCM is not offered over the GUI protocol")
        out = Path(path)
        if not out.parent.is_dir():
            raise InvalidInputError(f"folder does not exist: {out.parent}")
        if out.is_dir():
            raise InvalidInputError(f"{out} is a directory")
        if out.exists() and not req.get("overwrite", False):
            raise InvalidInputError(f"{out} already exists (overwrite not requested)")
        return out, fmt

    def synthesize(self, rid, req, cancel):
        from .backends.base import SynthParams
        from .encoders import INSTALL_HELP, encoder_status, parse_bitrate, resolve_mp3_settings
        from .engine import SpeechEngine, make_backend
        from .exceptions import BackendUnavailableError, EncoderUnavailableError, InvalidInputError
        from .text_preprocessor import iter_chunks
        from .voice_profiles import get_profile

        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidInputError("text is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidInputError(f"text too long ({len(text)} > {MAX_TEXT_CHARS} characters)")
        out, fmt = self._target(req)
        bitrate = parse_bitrate(req.get("bitrate_kbps"))
        if fmt == "mp3":
            resolve_mp3_settings(None, bitrate)
            if not encoder_status()["mp3"]:
                raise EncoderUnavailableError(INSTALL_HELP)
        backend = make_backend(req.get("backend") or DEFAULT_BACKEND)
        st = backend.status()
        if not st.available:
            raise BackendUnavailableError(f"backend '{st.name}' unavailable: " + "; ".join(st.problems))
        profile = get_profile(req.get("voice") or "original")
        overrides = {"speed": req.get("speed")}
        if req.get("language") and backend.name in ("local", "mock"):
            overrides["local_lang"] = req["language"]
        profile = profile.with_overrides(**overrides)
        from .emotions import apply_emotion
        profile = apply_emotion(profile, req.get("emotion"), req.get("emotion_intensity"))
        if req.get("session_dir"):
            return self.synthesize_session(rid, req, cancel, backend, profile, text, out, fmt)
        eng = SpeechEngine(backend, profile)
        total = sum(1 for _ in iter_chunks(text, max_chars=backend.max_chunk_chars,
                                           paragraph_pause_ms=profile.paragraph_pause_ms,
                                           sentence_pause_ms=profile.sentence_pause_ms)) or 1

        def on_progress(done, _t, _txt):
            self.progress(rid, done * 95 // total, f"chunk {done}/{total}")

        try:
            self.progress(rid, 0, "starting")
            info = eng.synthesize_to_file(text, out, fmt, SynthParams(speed=req.get("speed"), style=req.get("style") or None,
                                                       stretch=_stretch(req)),
                                          cancel=cancel, progress=on_progress, bitrate_kbps=bitrate)
        finally:
            eng.close()
        self.progress(rid, 100, "done")
        info.update(output_path=str(out), backend=backend.name, voice=profile.name, chunks=total)
        return info

    def export(self, rid, req, cancel):
        """Re-encode an already generated WAV to MP3 (or copy it as WAV) without re-synthesizing."""
        import shutil
        from .audio_pipeline import make_sink
        from .backends.base import AudioChunk
        from .encoders import INSTALL_HELP, encoder_status, parse_bitrate, resolve_mp3_settings
        from .exceptions import EncoderUnavailableError, InvalidInputError, SynthesisCancelled

        src = Path(req.get("input_path") or "")
        if not src.is_file():
            raise InvalidInputError(f"input audio not found: {src}")
        out, fmt = self._target(req)
        if out.exists() and out.resolve() == src.resolve():
            raise InvalidInputError("input and output are the same file")
        bitrate = parse_bitrate(req.get("bitrate_kbps"))
        try:
            w = wave.open(str(src), "rb")
        except (wave.Error, EOFError) as exc:
            raise InvalidInputError(f"input is not a readable WAV file: {exc}") from exc
        with w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise InvalidInputError("input WAV must be 16-bit mono")
            rate, nframes = w.getframerate(), w.getnframes()
            if fmt == "mp3":
                resolve_mp3_settings(None, bitrate)
                if not encoder_status()["mp3"]:
                    raise EncoderUnavailableError(INSTALL_HELP)
            else:
                tmp = out.with_name(f".{out.name}.{os.getpid()}.part")
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, out)
                finally:
                    tmp.unlink(missing_ok=True)
                self.progress(rid, 100, "done")
                return {"output_path": str(out), "format": "wav", "bytes": out.stat().st_size,
                        "duration_s": round(nframes / rate, 3), "sample_rate": rate}
            sink = make_sink(out, "mp3", None, bitrate)
            try:
                done = 0
                while True:
                    if cancel.is_set():
                        raise SynthesisCancelled("cancelled")
                    data = w.readframes(rate)       # one second per step
                    if not data:
                        break
                    sink.write(AudioChunk(data, rate))
                    done += len(data) // 2
                    self.progress(rid, done * 95 // max(nframes, 1), "encoding")
                sink.commit()
            except BaseException:
                sink.abort()
                raise
        self.progress(rid, 100, "done")
        return {"output_path": str(out), "format": "mp3", "bytes": out.stat().st_size,
                "sample_rate": sink.rate, "bitrate_kbps": sink.bitrate,
                "duration_s": round(sink.info["duration_s"], 3), "mp3_frames": sink.info["frames"]}


def _trim_working_set() -> None:
    """Windows: hand the worker's idle pages back to the OS (they are faulted in again only if touched). No-op elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
        k32.SetProcessWorkingSetSize(k32.GetCurrentProcess(), ctypes.c_size_t(-1).value, ctypes.c_size_t(-1).value)
    except Exception:
        pass


def _preload() -> None:
    """Import heavy native modules on the main thread before any job thread needs them.
    (Importing scipy for the first time from a worker thread while the main thread sat in a blocking
    stdin read deadlocked on Windows.)"""
    for mod in ("numpy", "scipy.signal", "lameenc"):
        try:
            __import__(mod)
        except Exception as exc:
            log(f"optional module {mod} not importable: {exc}")


def main() -> int:
    out = _Out()
    worker = Worker(out)
    log(f"started pid={os.getpid()} python={sys.version.split()[0]}")
    _preload()
    _trim_working_set()
    out.send({"event": "ready", "request_id": None, "pid": os.getpid()})
    lines: "queue.Queue[bytes | None]" = queue.Queue()

    def reader():
        stdin = sys.stdin.buffer
        while True:
            raw = stdin.readline()
            lines.put(raw or None)
            if not raw:
                return

    threading.Thread(target=reader, daemon=True).start()
    while True:
        raw = lines.get()
        if raw is None:                     # GUI closed the pipe: cancel and exit
            with worker.lock:
                if worker.job:
                    worker.job[1].set()
            log("stdin closed; exiting")
            return 0
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            worker.error(None, "MALFORMED_REQUEST", "request is not valid UTF-8")
            continue
        if line:
            worker.handle(line)


if __name__ == "__main__":
    sys.exit(main())
