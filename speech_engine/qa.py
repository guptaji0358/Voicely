"""Pronunciation-QA orchestration behind the worker actions: analysis, session synthesis, Recheck Speech,
targeted regeneration (review / auto repair), dictionary and feedback management.

Rules this module enforces:
  * the original audio is never overwritten (Session keeps every version; repairs are a separate candidate);
  * a repair is only reported as successful after the regenerated chunk was rechecked and the flagged term is no
    longer reported; if the voice ignores a hint, that is reported as a limitation instead of retrying forever;
  * auto-repair uses only approved sources: dictionary entries, candidates the user supplied in review mode, and
    validated LLM suggestions (when the LLM is enabled and network use was allowed). Nothing is guessed.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import __version__
from .asr import ASRProvider, make_asr
from .backends.base import Backend, SynthParams
from .exceptions import BackendUnavailableError, InvalidInputError, SynthesisCancelled
from .feedback import ConsentRequired, FeedbackStore, gate_change
from .llm.base import LLMConfig, LLMError
from .llm.service import LLMService
from .pron_dictionary import Application, Entry, PronDictionary
from .session import Session
from .text_analysis import analyze_text
from .verification import ChunkResult, Issue, Thresholds, verify_chunk, summarize
from dataclasses import asdict

REPAIRABLE = {"substitution", "pronunciation_discrepancy", "missing", "low_confidence"}


class QA:
    def __init__(self):
        self._dict: PronDictionary | None = None
        self._fb: FeedbackStore | None = None
        self._llm: LLMService | None = None
        self.make_asr = make_asr                  # injectable for tests

    # ---- shared objects -------------------------------------------------------------------
    @property
    def dictionary(self) -> PronDictionary:
        if self._dict is None:
            self._dict = PronDictionary()
        return self._dict

    def bind_ipa_vocab(self, backend: Backend) -> None:
        if backend.supports_ipa and self.dictionary.ipa_vocab is None:
            self.dictionary.ipa_vocab = backend.ipa_vocabulary()

    @property
    def feedback(self) -> FeedbackStore:
        if self._fb is None:
            self._fb = FeedbackStore()
        return self._fb

    @property
    def llm(self) -> LLMService:
        if self._llm is None:
            self._llm = LLMService(LLMConfig.load())
        return self._llm

    def versions(self, backend_name: str, asr: ASRProvider | None = None) -> dict:
        st = asr.status() if asr else None
        return {"engine": __version__, "dictionary": self.dictionary.version, "backend": backend_name,
                "asr_provider": st.provider if st else None, "asr_model": st.model if st else None,
                "llm_provider": self.llm.config.provider, "llm_model": self.llm.config.model,
                "prompt_version": self.llm.config.prompt_version}

    # ---- analysis ---------------------------------------------------------------------------
    def analyze(self, text: str, language: str, backend: Backend) -> dict:
        self.bind_ipa_vocab(backend)
        r = analyze_text(text, language, self.dictionary, ipa_ok=backend.supports_ipa)
        d = r.to_dict()
        d["controls"] = {"respelling": backend.supports_respelling, "ipa": backend.supports_ipa}
        d["dictionary_version"] = self.dictionary.version
        return d

    # ---- recheck ------------------------------------------------------------------------------
    @staticmethod
    def _qa_path(s: Session) -> Path:
        return s.dir / "qa.json"

    def load_results(self, s: Session) -> dict:
        p = self._qa_path(s)
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {"results": {}}
        except ValueError:
            return {"results": {}}

    def save_results(self, s: Session, doc: dict) -> None:
        p = self._qa_path(s)
        tmp = p.with_name(".qa.json.part")
        tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)

    def recheck(self, s: Session, which: str, chunk_ids: list[int] | None, asr: ASRProvider, th: Thresholds,
                cancel, progress) -> dict:
        st = asr.status()
        if not st.available:
            raise BackendUnavailableError("speech recognition is unavailable: " + "; ".join(st.problems))
        sel = s.selected(which)
        ids = chunk_ids if chunk_ids is not None else [c["index"] for c in s.chunks]
        doc = self.load_results(s)
        t0 = time.monotonic()
        results: list[ChunkResult] = []
        for n, i in enumerate(ids):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            if not 0 <= i < len(s.chunks):
                raise InvalidInputError(f"no chunk {i}")
            vid = sel[i]
            r = self._verify(s, i, vid, asr, th, cancel)
            doc["results"][str(i)] = {**r.to_dict(), "which": which}
            results.append(r)
            progress(int((n + 1) * 95 / len(ids)), f"checked {n + 1}/{len(ids)}")
        # report covers EVERY chunk with a stored result for the selected versions
        allr = [self._from_dict(doc["results"][str(i)]) for i in sorted(sel) if str(i) in doc["results"]
                and doc["results"][str(i)]["version"] == sel[i]]
        summary = summarize(allr, {"session_id": s.m["session_id"], "backend": s.m["backend"], "voice": s.m["voice"],
                                   "which": which, "elapsed_s": round(time.monotonic() - t0, 3),
                                   "dictionary_version": self.dictionary.version, "checked_all": len(allr) == len(s.chunks),
                                   "thresholds": asdict(th)})
        doc["summary"] = summary
        self.save_results(s, doc)
        return summary

    def _verify(self, s: Session, i: int, vid: str, asr, th, cancel) -> ChunkResult:
        c = s.chunks[i]
        return verify_chunk(i, vid, s.path_of(i, vid), c["text"], s.application_objects(i, vid), asr, s.m["language"], th,
                            self.dictionary, cancel)

    @staticmethod
    def _from_dict(d: dict) -> ChunkResult:
        d = {k: v for k, v in d.items() if k != "which"}
        d["issues"] = [Issue(**i) for i in d["issues"]]
        return ChunkResult(**d)

    # ---- repair -------------------------------------------------------------------------------------
    def regenerate(self, s: Session, backend: Backend, profile, chunk_ids: list[int] | None, mode: str,
                   candidates: list[dict], max_attempts: int, use_llm: bool, allow_network: bool,
                   asr: ASRProvider, th: Thresholds, cancel, progress) -> dict:
        if mode not in ("review", "auto"):
            raise InvalidInputError("mode must be 'review' or 'auto'")
        max_attempts = max(1, min(int(max_attempts), 5))
        params = SynthParams(speed=s.m.get("speed"), style=s.m.get("style"))
        doc = self.load_results(s)
        if chunk_ids is None:                          # every chunk whose current result needs attention
            chunk_ids = [int(i) for i, r in doc["results"].items() if r["status"] in ("needs_review", "failed")]
        if not chunk_ids:
            raise InvalidInputError("there is nothing to repair: run Recheck Speech first, or no chunk needs review")
        self.bind_ipa_vocab(backend)
        accepted = s.selected("accepted")
        cand_sel: dict[int, str] = dict(s.selected("candidate")) if s.m["selection"]["candidate"] else {}
        outcomes = []
        for n, idx in enumerate(sorted(set(chunk_ids))):
            if cancel.is_set():
                raise SynthesisCancelled("cancelled")
            progress(int(n * 90 / len(chunk_ids)), f"repairing chunk {idx + 1}")
            base_vid = cand_sel.get(idx, accepted[idx])
            r0 = doc["results"].get(str(idx))
            if r0 is None or r0["version"] != base_vid:
                r0 = self._verify(s, idx, base_vid, asr, th, cancel).to_dict()
                doc["results"][str(idx)] = r0
            outcomes.append(self._repair_chunk(s, idx, r0, backend, profile, params, mode, candidates, max_attempts,
                                               use_llm, allow_network, asr, th, cancel, doc, cand_sel))
        s.set_candidate(cand_sel or None)
        cand_path = s.assemble("candidate") if cand_sel else None
        # Report for the repaired mix (stored results of the selected versions); promoted to the main summary on accept.
        sel = s.selected("candidate" if cand_sel else "accepted")
        mix = [self._from_dict(doc["results"][str(i)]) for i in sorted(sel)
               if str(i) in doc["results"] and doc["results"][str(i)]["version"] == sel[i]]
        cand_summary = summarize(mix, {"session_id": s.m["session_id"], "backend": s.m["backend"], "voice": s.m["voice"],
                                       "which": "candidate" if cand_sel else "accepted",
                                       "dictionary_version": self.dictionary.version,
                                       "checked_all": len(mix) == len(s.chunks), "thresholds": asdict(th)})
        doc["candidate_summary"] = cand_summary if cand_sel else None
        self.save_results(s, doc)
        improved = [o for o in outcomes if o["status"] in ("repaired", "improved")]
        return {"session_id": s.m["session_id"], "mode": mode, "outcomes": outcomes, "summary": cand_summary,
                "candidate_path": str(cand_path) if cand_path else None,
                "original_path": str(s.dir / "assembled" / "original.wav"),
                "accepted_path": str(s.dir / "assembled" / "accepted.wav") if (s.dir / "assembled" / "accepted.wav").is_file() else None,
                "repaired_chunks": [o["chunk"] for o in improved],
                "unresolved_chunks": [o["chunk"] for o in outcomes if o["status"] == "unresolved"],
                "original_preserved": True,
                "note": "The original audio is unchanged. Listen to the repaired version and accept it, or keep the original."}

    def _repair_chunk(self, s, idx, r0, backend, profile, params, mode, user_cands, max_attempts, use_llm, allow_network,
                      asr, th, cancel, doc, cand_sel) -> dict:
        chunk = s.chunks[idx]
        text = chunk["text"]
        issues = [i for i in r0["issues"] if i["severity"] == "review"]
        terms = []
        for i in issues:
            if i["type"] in REPAIRABLE and i["intended"] and i["orig_start"] is not None:
                if i["intended"].lower() not in [t["term"].lower() for t in terms]:
                    terms.append({"term": i["intended"], "issue": i})
        out = {"chunk": idx, "status": "unresolved", "attempts": [], "limitations": [], "terms": [t["term"] for t in terms],
               "version": None}
        unrepairable = [i for i in issues if i["type"] not in REPAIRABLE]
        if unrepairable and not terms:
            out["limitations"].append("The remaining findings (e.g. words a transcript cannot confirm, acoustic problems) "
                                      "cannot be repaired automatically; listen to the segment.")
            return out
        if not terms:
            out["limitations"].append("No specific word was identified in this chunk, so there is nothing to target.")
            return out
        any_resolved = False
        base = s.version(idx, cand_sel.get(idx, s.selected('accepted')[idx]))
        overlay_apps: list[Application] = [Application(**a) for a in base["applications"]]
        extra_overrides: list[tuple[str, str]] = []
        tried: set[tuple] = set()
        last_recognized = [r0["recognized"]]
        for t in terms:
            cands = self._candidates(t["term"], text, mode, user_cands, idx, backend, use_llm, allow_network, cancel, out["limitations"],
                                     s.m["language"])
            if not cands:
                out["limitations"].append(
                    f"No supported correction is available for '{t['term']}': " +
                    ("this voice/backend takes no IPA and no respelling candidate exists. " if not backend.supports_ipa else "") +
                    "Add a respelling (and IPA where supported) in the dictionary and run the repair again.")
                continue
            for cand in cands[:max_attempts]:
                key = (t["term"].lower(), cand.get("hint", ""), cand.get("ipa", ""))
                if key in tried:
                    continue
                tried.add(key)
                if cancel.is_set():
                    raise SynthesisCancelled("cancelled")
                apps = self._overlay_for(text, t["term"], cand, backend)
                if not apps:
                    out["limitations"].append(f"candidate for '{t['term']}' needs IPA but this backend cannot use IPA")
                    continue
                v = s.regenerate_chunk(idx, backend, profile, params, self.dictionary, apps, None,
                                       f"repair:{'ipa' if apps[0].mode == 'ipa' else 'respelling'}", cancel)
                res = self._verify(s, idx, v["id"], asr, th, cancel)
                doc["results"][str(idx)] = {**res.to_dict(), "which": "candidate"}
                still = [i for i in res.issues if i.severity == "review" and i.type in REPAIRABLE
                         and i.orig_start is not None and i.orig_start < apps[0].end and (i.orig_end or 0) > apps[0].start]
                resolved = not still and res.status != "failed"
                out["attempts"].append({"term": t["term"], "candidate": {"hint": cand.get("hint", ""), "ipa": cand.get("ipa", ""),
                                                                        "source": cand.get("source", "")},
                                        "version": v["id"], "recheck_status": res.status, "recognized": res.recognized,
                                        "term_resolved": resolved, "spoken": v["spoken"]})
                if resolved:
                    cand_sel[idx] = v["id"]
                    out["version"] = v["id"]
                    any_resolved = True
                    out["status"] = "repaired" if res.status == "verified" else "improved"
                    out["resolved_term"] = t["term"]
                    out["suggested_entry"] = {"written": t["term"], **{k: cand.get(k, "") for k in ("hint", "ipa")},
                                              "category": "name"}
                    break
                if res.recognized == last_recognized[-1]:
                    out["limitations"].append(
                        f"The voice produced the same result for '{t['term']}' after this change: the {backend.name} backend "
                        "does not seem to honour it. Further substitutions were not tried.")
                    break
                last_recognized.append(res.recognized)
        if not any_resolved and out["attempts"]:
            out["limitations"].append("None of the attempted corrections was confirmed by the recheck; the original is kept.")
        return out

    def _candidates(self, term, text, mode, user_cands, idx, backend, use_llm, allow_network, cancel, limits, language) -> list[dict]:
        c = []
        for u in user_cands:
            if (u.get("chunk") in (None, idx)) and u.get("written", "").lower() == term.lower() and (u.get("hint") or u.get("ipa")):
                c.append({"hint": u.get("hint", ""), "ipa": u.get("ipa", "") if backend.supports_ipa else "", "source": "user"})
        for e in self.dictionary.entries.values():
            if e.written.lower() == term.lower() and e.enabled and (e.ipa or e.spoken_text()):
                c.append({"hint": e.spoken_text(), "ipa": e.ipa if backend.supports_ipa else "", "source": "dictionary"})
        if mode == "auto" and use_llm:
            try:
                res = self.llm.suggest_fix(term, text, "", {"respelling": True, "ipa": backend.supports_ipa, "language": language},
                                           allow_network, cancel, backend.ipa_vocabulary())
                for sgs in res["suggestions"]:
                    c.append({"hint": sgs["respelling"], "ipa": sgs["ipa"], "source": "llm"})
            except LLMError as exc:
                limits.append(f"LLM suggestions unavailable ({exc.code}): {exc}")
        seen, uniq = set(), []
        for x in c:
            k = (x["hint"], x["ipa"])
            if k not in seen and (x["hint"] or x["ipa"]):
                seen.add(k)
                uniq.append(x)
        return uniq

    @staticmethod
    def _overlay_for(text: str, term: str, cand: dict, backend: Backend) -> list[Application]:
        import re
        apps = []
        for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.IGNORECASE):
            if cand.get("ipa") and backend.supports_ipa:
                apps.append(Application(m.start(), m.end(), m.group(), "temp", m.group(), "ipa", cand["ipa"]))
            elif cand.get("hint"):
                apps.append(Application(m.start(), m.end(), m.group(), "temp", cand["hint"], "text", cand.get("ipa", "")))
        return apps

    # ---- dictionary ops ---------------------------------------------------------------------------------
    def dictionary_op(self, req: dict, backend_for_ipa: Backend | None = None) -> dict:
        op = req.get("op")
        d = self.dictionary
        if backend_for_ipa is not None:
            self.bind_ipa_vocab(backend_for_ipa)
        if op == "list" or op == "search":
            es = d.search(req.get("query", ""), req.get("category"))
            return {"entries": [asdict(e) for e in es], "version": d.version, "count": len(d.entries)}
        if op == "add":
            fields_ = {k: v for k, v in (req.get("entry") or {}).items()}
            gate = None
            if req.get("check_regression", True):
                tmp = d._build(dict(fields_))
                tmp.validate(d.ipa_vocab)
                gate = gate_change(d, self.feedback, add=tmp)
                if not gate["ok"]:
                    return {"added": False, "regression": gate, "version": d.version}
            ent = d.add(**fields_)
            return {"added": True, "entry": asdict(ent), "version": d.version, "regression": gate}
        if op == "update":
            fields_ = dict(req.get("entry") or {})
            gate = None
            if req.get("check_regression", True):
                old = d.entries.get(req.get("id"))
                if old is None:
                    raise InvalidInputError(f"no dictionary entry {req.get('id')!r}")
                tmp = d._build({**asdict(old), **{k: v for k, v in fields_.items() if k not in ("id", "created")}})
                tmp.validate(d.ipa_vocab)
                gate = gate_change(d, self.feedback, add=tmp)
                if not gate["ok"]:
                    return {"updated": False, "regression": gate, "version": d.version}
            ent = d.update(req.get("id"), **fields_)
            return {"updated": True, "entry": asdict(ent), "version": d.version, "regression": gate}
        if op == "delete":
            gate = gate_change(d, self.feedback, remove_id=req.get("id")) if req.get("check_regression", True) else None
            if gate and not gate["ok"] and not req.get("force"):
                return {"deleted": False, "regression": gate, "version": d.version}
            d.delete(req.get("id"))
            return {"deleted": True, "version": d.version}
        if op == "export":
            fmt = req.get("format", "json")
            text = d.export(fmt)
            path = req.get("path")
            if path:
                Path(path).write_text(text, encoding="utf-8")
                return {"path": path, "count": len(d.entries), "format": fmt}
            return {"text": text, "count": len(d.entries), "format": fmt}
        if op == "import":
            fmt = req.get("format", "json")
            if req.get("path"):
                text = Path(req["path"]).read_text(encoding="utf-8-sig")
            else:
                text = req.get("text", "")
            return d.import_text(text, fmt, bool(req.get("replace_existing")))
        raise InvalidInputError(f"unknown dictionary op {op!r}")

    # ---- feedback ops --------------------------------------------------------------------------------------
    def feedback_op(self, req: dict) -> dict:
        f = self.feedback
        op = req.get("op")
        if op == "status":
            return {"consent": f.consent(), **f.stats()}
        if op == "set_consent":
            return {"consent": f.set_consent(req.get("store_feedback"), req.get("store_audio"))}
        if op == "record":
            rec = f.record(req.get("record") or {}, req.get("audio_path"))
            return {"record": rec}
        if op == "list":
            return {"records": f.list(req.get("query", ""))}
        if op == "delete":
            f.delete(req.get("id"))
            return {"deleted": 1}
        if op == "delete_all":
            return {"deleted": f.delete_all()}
        if op == "export":
            return f.export(req.get("dest_dir"), bool(req.get("include_audio", True)))
        if op == "regression":
            from .feedback import build_cases, evaluate
            return evaluate(list(self.dictionary.entries.values()), build_cases(f))
        raise InvalidInputError(f"unknown feedback op {op!r}")
