"""Protocol-2 actions (pronunciation QA, dictionary, feedback, LLM, ASR) as a mixin for ``worker.Worker``.
Keeps worker.py focused on framing, dispatch and the original synthesis/export jobs."""

from __future__ import annotations

import os
import shutil
import sys
import wave

from .worker_defaults import DEFAULT_BACKEND, stretch_of as _stretch
from .exceptions import (BackendUnavailableError, InvalidInputError, SpeechEngineError)

MAX_TEXT_CHARS = 2_000_000


class QAMixin:
    # ---- helpers ---------------------------------------------------------------------------
    def _open_session(self, req):
        from .session import Session
        d = req.get("session_dir")
        if not isinstance(d, str) or not d:
            raise InvalidInputError("session_dir is required")
        return Session.open(d)

    def _session_backend(self, s):
        from .engine import make_backend
        from .voice_profiles import get_profile
        backend = make_backend(s.m["backend"])
        st = backend.status()
        if not st.available:
            raise BackendUnavailableError(f"backend '{st.name}' unavailable: " + "; ".join(st.problems))
        overrides = {"speed": s.m.get("speed")}
        if s.m["backend"] in ("local", "mock"):
            overrides["local_lang"] = s.m["language"]
        from .emotions import apply_emotion
        profile = get_profile(s.m["voice"]).with_overrides(**overrides)
        return backend, apply_emotion(profile, s.m.get("emotion"), s.m.get("emotion_intensity"))

    def _thresholds(self, req):
        from .verification import Thresholds
        return Thresholds.from_dict(req.get("thresholds"))

    # ---- jobs ----------------------------------------------------------------------------------
    def synthesize_session(self, rid, req, cancel, backend, profile, text, out, fmt):
        from .backends.base import SynthParams
        from .session import Session
        if fmt != "wav":
            raise InvalidInputError("a speech session is always written as WAV; export to MP3 afterwards")
        qa = self.qa
        qa.bind_ipa_vocab(backend)
        language = req.get("language") or profile.local_lang or "en-us"

        def on_progress(done, total_chunks, _txt):
            self.progress(rid, done * 95 // max(total_chunks, 1), f"chunk {done}/{total_chunks}")

        self.progress(rid, 0, "starting")
        s = Session.create(req["session_dir"], text, backend, profile,
                           SynthParams(speed=req.get("speed"), style=req.get("style") or None,
                                   stretch=_stretch(req)), language,
                           qa.dictionary if req.get("dictionary", True) else None, cancel, on_progress,
                           qa.versions(backend.name),
                           emotion=(req.get("emotion"), req.get("emotion_intensity")))
        try:                                   # opt-in learning record; it must never be able to break a generation
            from .experience import ExperienceStore
            ExperienceStore().add_generation(s.m["session_id"], s.chunks, language, profile.name, backend.name)
        except Exception as exc:
            print(f"[worker] experience capture skipped: {exc}", file=sys.stderr, flush=True)
        orig = s.assemble("original", cancel=cancel)
        tmp = out.with_name(f".{out.name}.{os.getpid()}.part")
        try:
            shutil.copyfile(orig, tmp)
            os.replace(tmp, out)
        finally:
            tmp.unlink(missing_ok=True)
        with wave.open(str(out)) as w:
            rate, frames = w.getframerate(), w.getnframes()
        self.progress(rid, 100, "done")
        napps = sum(len(c["versions"][0]["applications"]) for c in s.chunks)
        return {"output_path": str(out), "format": "wav", "bytes": out.stat().st_size,
                "duration_s": round(frames / rate, 3), "sample_rate": rate, "backend": backend.name,
                "voice": profile.name, "chunks": len(s.chunks),
                "session": {"id": s.m["session_id"], "dir": str(s.dir), "manifest": str(s.manifest_path),
                            "dictionary_version": s.m["dictionary_version"], "dictionary_applications": napps,
                            "original_path": str(orig)}}

    def recheck(self, rid, req, cancel):
        s = self._open_session(req)
        which = req.get("which", "accepted")
        if which not in ("accepted", "candidate", "original"):
            raise InvalidInputError("which must be accepted, candidate or original")
        ids = req.get("chunks")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
            raise InvalidInputError("chunks must be a list of integers")
        asr = self.qa.make_asr(req.get("asr"))
        self.progress(rid, 0, "loading speech recogniser")
        summary = self.qa.recheck(s, which, ids, asr, self._thresholds(req), cancel, lambda p, m: self.progress(rid, p, m))
        self.progress(rid, 100, "done")
        return {"summary": summary}

    def regenerate(self, rid, req, cancel):
        s = self._open_session(req)
        backend, profile = self._session_backend(s)
        asr = self.qa.make_asr(req.get("asr"))
        cands = req.get("candidates") or []
        if not isinstance(cands, list) or not all(isinstance(c, dict) for c in cands):
            raise InvalidInputError("candidates must be a list of objects")
        ids = req.get("chunks")
        if ids is not None and (not isinstance(ids, list) or not all(isinstance(i, int) for i in ids)):
            raise InvalidInputError("chunks must be a list of integers")
        try:
            res = self.qa.regenerate(s, backend, profile, ids, req.get("mode", "review"), cands,
                                     req.get("max_attempts", 3), bool(req.get("use_llm")), bool(req.get("allow_network")),
                                     asr, self._thresholds(req), cancel, lambda p, m: self.progress(rid, p, m))
        finally:
            backend.close()
        self.progress(rid, 100, "done")
        return res

    def llm_analyze(self, rid, req, cancel):
        from .engine import make_backend
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidInputError("text is empty")
        backend = make_backend(req.get("backend") or DEFAULT_BACKEND)
        self.qa.bind_ipa_vocab(backend)
        self.progress(rid, 5, "asking the language model")
        examples = self.qa.feedback.approved_examples() if self.qa.llm.config.use_examples else None
        res = self.qa.llm.analyze(text, req.get("language") or "en-us", {"respelling": True, "ipa": backend.supports_ipa},
                                  bool(req.get("allow_network")), cancel, examples, backend.ipa_vocabulary())
        self.progress(rid, 100, "done")
        return res

    def llm_explain(self, rid, req, cancel):
        return self.qa.llm.explain(str(req.get("intended", "")), str(req.get("recognized", "")),
                                   str(req.get("context", "")), bool(req.get("allow_network")), cancel)

    def asr_download(self, rid, req, cancel):
        from .asr import download_model
        if req.get("confirm") is not True:
            raise InvalidInputError("downloading the speech-recognition model requires confirm:true (it uses the network)")
        self.progress(rid, 5, "downloading model")
        return {"path": download_model(req.get("model"))}

    # ---- synchronous actions ------------------------------------------------------------------------
    def analyze_text(self, rid, req):
        from .engine import make_backend
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise InvalidInputError("text is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise InvalidInputError("text too long")
        return self.qa.analyze(text, req.get("language") or "en-us", make_backend(req.get("backend") or DEFAULT_BACKEND))

    def sync_action(self, rid, action, req):
        from .feedback import ConsentRequired
        from .llm.base import LLMError
        try:
            if action == "analyze_text":
                res = self.analyze_text(rid, req)
            elif action == "dictionary":
                from .engine import make_backend
                b = make_backend(req["backend"]) if req.get("backend") else None
                res = self.qa.dictionary_op(req, b)
            elif action == "feedback":
                res = self.qa.feedback_op(req)
            elif action == "llm_status":
                res = self.qa.llm.status()
            elif action == "llm_configure":
                self.qa.llm.config.update(**(req.get("settings") or {}))
                self.qa.llm._provider = None
                res = self.qa.llm.status()
            elif action == "asr_status":
                st = self.qa.make_asr(req.get("asr")).status()
                res = {"provider": st.provider, "available": st.available, "model": st.model,
                       "details": st.details, "problems": st.problems}
            elif action == "google_usage":
                from .google_usage import UsageTracker
                res = UsageTracker().snapshot()
            elif action == "session_info":
                res = self.session_info(req)
            elif action == "segment":
                res = self.segment(req)
            elif action == "accept_repair":
                s = self._open_session(req)
                path = s.accept_candidate()
                doc = self.qa.load_results(s)
                if doc.get("candidate_summary"):                 # the repaired mix was rechecked chunk by chunk
                    doc["summary"] = doc.pop("candidate_summary")
                    doc["summary"]["which"] = "accepted"
                    self.qa.save_results(s, doc)
                res = {"accepted_path": str(path), "original_path": str(s.dir / "assembled" / "original.wav"),
                       "summary": doc.get("summary")}
            else:   # discard_repair
                s = self._open_session(req)
                s.discard_candidate()
                doc = self.qa.load_results(s)
                doc["candidate_summary"] = None
                self.qa.save_results(s, doc)
                res = {"discarded": True, "summary": doc.get("summary")}
            self.ok(rid, **res)
        except LLMError as exc:
            self.error(rid, exc.code, str(exc))
        except ConsentRequired as exc:
            self.error(rid, "CONSENT_REQUIRED", str(exc))
        except InvalidInputError as exc:
            self.error(rid, "INVALID_INPUT", str(exc))
        except BackendUnavailableError as exc:
            self.error(rid, "BACKEND_UNAVAILABLE", str(exc))
        except SpeechEngineError as exc:
            self.error(rid, "SYNTHESIS_FAILED", str(exc))

    def session_info(self, req):
        s = self._open_session(req)
        doc = self.qa.load_results(s)
        sel = s.selected("accepted")
        cand = s.selected("candidate") if s.m["selection"]["candidate"] else None
        chunks = [{"index": c["index"], "text": c["text"], "source_span": c["source_span"],
                   "versions": [v["id"] for v in c["versions"]], "accepted": sel[c["index"]],
                   "candidate": cand[c["index"]] if cand and cand[c["index"]] != sel[c["index"]] else None}
                  for c in s.chunks]
        asm = s.dir / "assembled"
        return {"session_id": s.m["session_id"], "backend": s.m["backend"], "voice": s.m["voice"],
                "language": s.m["language"], "chunks": chunks, "summary": doc.get("summary"),
                "versions": s.m.get("versions", {}), "original_path": str(asm / "original.wav"),
                "accepted_path": str(asm / "accepted.wav") if (asm / "accepted.wav").is_file() else str(asm / "original.wav"),
                "candidate_path": str(asm / "candidate.wav") if cand and (asm / "candidate.wav").is_file() else None}

    def segment(self, req):
        s = self._open_session(req)
        i = req.get("chunk")
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(s.chunks):
            raise InvalidInputError("chunk index out of range")
        vid = req.get("version") or s.selected(req.get("which", "accepted"))[i]
        v = s.version(i, vid)
        return {"path": str(s.path_of(i, vid)), "chunk": i, "version": vid, "duration_s": v["duration_s"],
                "text": s.chunks[i]["text"], "spoken": v["spoken"], "method": v["method"]}
