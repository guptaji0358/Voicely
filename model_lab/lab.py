"""High-level Model Lab operations shared by the CLI, the worker (Qt) and the tests.

    make dataset -> init model (random) -> train -> evaluate -> [promote with approval] | expand -> train again -> ...

Every operation that creates a checkpoint writes a NEW file and a NEW registry entry; nothing is overwritten and the previous
production model stays available for rollback.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from pathlib import Path

from . import memory, synth
from .checkpoint import CheckpointError, load_checkpoint, model_from_checkpoint, save_checkpoint
from .config import ModelConfig, preset
import shutil

from .data import (NEEDS_REVIEW_LABELS, SEP, SPLITS, TASK_TAGS, TokenSet, build_dataset, import_experience_records, list_datasets,
                   load_dataset, tokenize_split)
from .evaluate import Criteria, decide, evaluate_split
from .expand import expand_checkpoint
from .registry import Registry, RegistryError, lab_dir
from .tokenizer import Tokenizer
from .train import Trainer, TrainConfig, TrainingError


class LabError(Exception):
    pass


# The vocabulary is fixed for the life of a model, so it must already cover ordinary text (printable ASCII, a few typographic
# characters, the task tags and separators), not just the characters of the first dataset.
BASE_ALPHABET = "".join(chr(c) for c in range(32, 127)) + "’‘“”–—…éèàüöäñç"


class Lab:
    def __init__(self, root: Path | None = None):
        self.registry = Registry(root)
        self.root = self.registry.root
        self.datasets_dir = self.root / "datasets"
        self.runs_dir = self.root / "runs"

    # ---- datasets ---------------------------------------------------------------------------------
    def make_synthetic_dataset(self, name: str = "review-category", n_per_label: int = 400, seed: int = 7) -> dict:
        main = synth.generate(n_per_label, seed, "train")
        reg = synth.generate(max(20, n_per_label // 8), seed + 1, "heldout")
        return build_dataset(self.datasets_dir, name, main, synth.LABELS, "review_category", seed, regression=reg,
                             provenance=("SYNTHETIC: labels correct by construction from model_lab/synth.py templates; regression split "
                                         "uses trigger words that never appear in train/val/test. Not user data."))

    def datasets(self) -> list[dict]:
        return list_datasets(self.datasets_dir)

    def prepare_tokens(self, dataset_path: Path, tok: Tokenizer, max_seq: int) -> str:
        """Tokenise every split into memory-mapped arrays inside the dataset folder (a cache; keyed by tokenizer + length)."""
        tag = hashlib.sha256((json.dumps(tok.to_dict(), sort_keys=True) + f"|{max_seq}").encode()).hexdigest()[:8]
        manifest, splits = load_dataset(dataset_path)
        if not (Path(dataset_path) / f"tokens_{tag}_train.npy").exists():
            for s in SPLITS:
                tokenize_split(tok, splits[s], max_seq, Path(dataset_path), s, tag)
        return tag

    # ---- models -----------------------------------------------------------------------------------
    def _next_version(self, name: str) -> int:
        """Next free version number for ``name`` (entries may have been deleted, so counting them would reuse numbers)."""
        return max((m["version"] for m in self.registry.models() if m["name"] == name), default=0) + 1

    def model_dir(self, name: str) -> Path:
        return self.registry.checkpoint_dir(name)

    def tokenizer_for(self, entry: dict) -> Tokenizer:
        return Tokenizer.load(self.model_dir(entry["name"]) / "tokenizer.json")

    def _plan_init(self, dataset_path: Path, size: str | None, cfg: ModelConfig | None, max_seq: int, precision: str, confirmed: bool):
        """Validate everything about a new model (alphabet, architecture, memory) WITHOUT creating or deleting anything."""
        manifest, splits = load_dataset(dataset_path)
        extra = set("".join(r["text"] for r in splits["train"]) + "".join(manifest["labels"]) + SEP + "".join(TASK_TAGS.values()))
        tok = Tokenizer.from_alphabet(BASE_ALPHABET + "".join(sorted(extra - set(BASE_ALPHABET))))
        cfg = cfg or preset(size, tok.size, max_seq, precision)
        if cfg.vocab_size != tok.size:
            raise LabError(f"config vocab_size {cfg.vocab_size} does not match the tokenizer ({tok.size})")
        est = memory.estimate_training_bytes(cfg, 32, seq_len=min(cfg.max_seq, 128))
        dec = memory.check_budget(est["total"], confirmed=confirmed)
        if dec.verdict != "ok":
            raise LabError(f"cannot instantiate {cfg.param_count():,} parameters: {dec.reason}")
        return manifest, tok, cfg

    def init_model(self, name: str, dataset_path: Path, size: str | None = "100K", cfg: ModelConfig | None = None,
                   max_seq: int = 448, seed: int = 0, precision: str = "fp32", confirmed: bool = False) -> dict:
        """Create a randomly initialised model + tokenizer over the fixed alphabet. Loads nothing pretrained."""
        from .model import build_model
        manifest, tok, cfg = self._plan_init(dataset_path, size, cfg, max_seq, precision, confirmed)
        model = build_model(cfg, seed)
        d = self.model_dir(name)
        if not (d / "tokenizer.json").exists():
            tok.save(d / "tokenizer.json")
        else:
            tok = Tokenizer.load(d / "tokenizer.json")
            if tok.size != cfg.vocab_size:
                raise LabError("an existing model with this name uses a different tokenizer; choose another name")
        version = self._next_version(name)
        info = save_checkpoint(d / f"v{version}-random-init.pt", model, cfg,
                               meta={"origin": "scratch", "seed": seed, "pretrained": False, "dataset_version": manifest["version"]})
        return self.registry.register(name=name, config=cfg.to_dict(), param_count=cfg.param_count(), checkpoint=info["path"],
                                      sha256=info["sha256"], state="random_init", seed=seed, dataset_version=manifest["version"],
                                      steps=0, notes={"initialised_from": "random (seed recorded); no pretrained weights",
                                                      "steps_at_expansion": None})

    # ---- training ---------------------------------------------------------------------------------
    def estimate(self, model_id: str, batch_size: int, optimizer: str = "adamw") -> dict:
        e = self.registry.get(model_id)
        cfg = ModelConfig.from_dict(e["config"])
        est = memory.estimate_training_bytes(cfg, batch_size, optimizer=optimizer)
        return {**est, "decision": memory.check_budget(est["total"]).to_dict()}

    def train(self, model_id: str, dataset_path: Path, tc: TrainConfig, *, confirmed: bool = False,
              cancel: threading.Event | None = None, on_event=None, resume_run: str | None = None) -> dict:
        """Train from the checkpoint of ``model_id`` (or resume a run). Creates a NEW registry entry on completion."""
        parent = self.registry.get(model_id)
        cfg = ModelConfig.from_dict(parent["config"])
        tok = self.tokenizer_for(parent)
        manifest, _ = load_dataset(dataset_path)
        tag = self.prepare_tokens(dataset_path, tok, cfg.max_seq)
        train, val = TokenSet(dataset_path, "train", tag), TokenSet(dataset_path, "val", tag)
        est = memory.estimate_training_bytes(cfg, tc.batch_size, seq_len=max(8, train.longest()))
        dec = memory.check_budget(est["total"], confirmed=confirmed)
        if dec.verdict != "ok":
            raise LabError(("CONFIRMATION_NEEDED: " if dec.verdict == "needs_confirmation" else "REFUSED: ") + dec.reason)
        ck = load_checkpoint(parent["checkpoint"], parent["sha256"])
        model, _ = model_from_checkpoint(ck)
        runs = self.runs_dir / parent["name"]
        n = len(list(runs.glob("run-*"))) + 1 if runs.exists() else 1
        run_dir = Path(resume_run) if resume_run else runs / f"run-{n:03d}"
        meta = {"model_id": model_id, "dataset_version": manifest["version"], "parent_checkpoint": parent["checkpoint"]}
        trainer = Trainer(model, cfg, tc, train, val, run_dir, meta)
        if resume_run:
            trainer.resume(run_dir / "latest.pt")
        result = trainer.run(cancel, on_event)
        result["estimated_peak_bytes"] = est["total"]
        result["run_dir"] = str(run_dir)
        result["dataset_version"] = manifest["version"]
        if result["status"] in ("completed", "early_stopped"):
            d = self.model_dir(parent["name"])
            version = self._next_version(parent["name"])
            steps_total = parent["steps"] + result["steps_this_run"]
            info = save_checkpoint(d / f"v{version}-step{steps_total}.pt", model, cfg,
                                   meta={"origin": "scratch", "parent": model_id, "dataset_version": manifest["version"],
                                         "train_config": asdict(tc), "result": {k: v for k, v in result.items() if k != "train_config"}})
            notes = dict(parent.get("notes") or {})
            notes.pop("role", None)                      # the caller decides whether this is a candidate or the active model
            notes["training"] = {k: result[k] for k in ("status", "steps_this_run", "final_train_loss", "best_val_loss", "elapsed_s",
                                                         "peak_rss_bytes", "estimated_peak_bytes", "device", "gradient_check")}
            entry = self.registry.register(name=parent["name"], config=parent["config"], param_count=parent["param_count"],
                                           checkpoint=info["path"], sha256=info["sha256"], state="trained", seed=parent["seed"],
                                           parent=model_id, dataset_version=manifest["version"], steps=steps_total,
                                           metrics={"final_train_loss": result["final_train_loss"],
                                                    "best_val_loss": result["best_val_loss"]}, notes=notes, origin=parent["origin"])
            result["model"] = entry
        return result

    # ---- evaluation / promotion -------------------------------------------------------------------
    def _measure(self, entry: dict, dataset_path: Path, limit: int | None = None) -> tuple[dict, dict]:
        """Real metrics of one registered model on one dataset version: {split: metrics}, manifest."""
        ck = load_checkpoint(entry["checkpoint"], entry["sha256"])
        model, cfg = model_from_checkpoint(ck)
        tok = self.tokenizer_for(entry)
        manifest, splits = load_dataset(dataset_path)
        tag = self.prepare_tokens(dataset_path, tok, cfg.max_seq)
        res = {}
        for s, lim in (("train", 600), ("val", limit), ("test", limit), ("regression", limit)):
            res[s] = evaluate_split(model, tok, TokenSet(dataset_path, s, tag), splits[s], manifest["labels"], limit=lim,
                                    task=manifest["task"])
        return res, manifest

    def evaluate(self, model_id: str, dataset_path: Path, criteria: Criteria | None = None, limit: int | None = None) -> dict:
        entry = self.registry.get(model_id)
        res, manifest = self._measure(entry, dataset_path, limit)
        best = self.registry.status()["best"]
        best_metrics = None
        if best and best != model_id:
            bm = self.registry.get(best)["metrics"]
            best_metrics = bm.get("evaluation") if isinstance(bm, dict) else None
            if best_metrics and best_metrics.get("dataset") != manifest["version"]:
                best_metrics = None          # metrics measured on another dataset version are not comparable
        verdict = decide(res, best_metrics.get("results") if best_metrics else None, criteria)
        notes = entry.get("notes") or {}
        ancestor_steps = notes.get("steps_at_expansion")
        if ancestor_steps is not None and entry["steps"] <= ancestor_steps:
            verdict["checks"].append({"name": "trained_after_expansion", "ok": False,
                                      "detail": "this expanded model has not been trained since expansion; it cannot be accepted yet"})
            verdict["passed"] = False
        state = "eval_passed" if verdict["passed"] else "eval_failed"
        metrics = dict(entry["metrics"])
        metrics["evaluation"] = {"dataset": manifest["version"], "results": res, "verdict": verdict}
        self.registry.update(model_id, state=state, metrics=metrics)
        if verdict["passed"]:
            self.registry.set_best(model_id)
        return {"model_id": model_id, "state": state, "dataset": manifest["version"], "results": res, "verdict": verdict}

    def promote(self, model_id: str, approved: bool, approver: str = "") -> dict:
        return self.registry.promote(model_id, approved, approver)

    def rollback(self) -> dict:
        return self.registry.rollback()

    # ---- expansion --------------------------------------------------------------------------------
    def expand(self, model_id: str, target_params: int, seed: int = 0, confirmed: bool = False) -> dict:
        """Create a NEW larger model from ``model_id``. The source checkpoint and registry entry are untouched."""
        src = self.registry.get(model_id)
        ck = load_checkpoint(src["checkpoint"], src["sha256"])
        from .expand import plan_to_target
        new_cfg = plan_to_target(ModelConfig.from_dict(src["config"]), target_params)
        est = memory.estimate_training_bytes(new_cfg, 8)
        dec = memory.check_budget(est["total"], confirmed=confirmed)
        if dec.verdict == "refuse":
            raise LabError(f"REFUSED: expanding to {new_cfg.param_count():,} parameters: {dec.reason}")
        new_model, new_cfg, report = expand_checkpoint(ck, new_cfg=new_cfg, seed=seed)
        report["requested_target_params"] = target_params
        d = self.model_dir(src["name"])
        version = self._next_version(src["name"])
        info = save_checkpoint(d / f"v{version}-expanded-{new_cfg.param_count()}.pt", new_model, new_cfg,
                               meta={"origin": "expanded", "parent": model_id, "expansion": {k: v for k, v in report.items() if k != "tensors"}})
        (d / f"v{version}-expansion-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        entry = self.registry.register(name=src["name"], config=new_cfg.to_dict(), param_count=new_cfg.param_count(),
                                       checkpoint=info["path"], sha256=info["sha256"], state="expanded", seed=src["seed"],
                                       parent=model_id, dataset_version=src["dataset_version"], steps=src["steps"], origin="expanded",
                                       notes={"expansion": {k: v for k, v in report.items() if k not in ("tensors", "old_config", "new_config")},
                                              "steps_at_expansion": src["steps"],
                                              "requires_training": True})
        return {"model": entry, "report": {k: v for k, v in report.items() if k != "tensors"}, "memory_decision": dec.to_dict()}

    # =====================================================================================================
    # Single-model workflow: there is ONE active model (created by the user). Training makes a temporary
    # candidate; the person accepts it (it replaces the model and the old files are deleted) or discards it.
    # =====================================================================================================
    def _by_role(self, role: str) -> list[dict]:
        return [m for m in self.registry.models() if (m.get("notes") or {}).get("role") == role]

    def active(self) -> dict | None:
        found = self._by_role("active")
        return found[-1] if found else None

    def candidate(self) -> dict | None:
        found = self._by_role("candidate")
        return found[-1] if found else None

    def _set_role(self, model_id: str, role: str) -> dict:
        e = self.registry.get(model_id)
        return self.registry.update(model_id, notes={**(e.get("notes") or {}), "role": role})

    def _drop(self, entry: dict) -> None:
        """Delete one model's checkpoint file, its expansion report (if any) and its registry entry."""
        ck = Path(entry["checkpoint"])
        for f in (ck, *ck.parent.glob(ck.stem.split("-")[0] + "-expansion-report.json")):
            try:
                f.unlink()
            except OSError:
                pass
        self.registry.delete(entry["id"])

    def _forget_runs(self, name: str) -> None:
        shutil.rmtree(self.runs_dir / name, ignore_errors=True)

    def cleanup_models(self, keep_id: str | None = None) -> dict:
        """Delete every model except ``keep_id`` (files, runs and registry entries). Returns what was removed."""
        removed = []
        for m in list(self.registry.models()):
            if m["id"] != keep_id:
                self._drop(m)
                removed.append({"id": m["id"], "name": m["name"], "version": m["version"], "params": m["param_count"]})
        keep_names = {self.registry.get(keep_id)["name"]} if keep_id else set()
        mdir = self.root / "models"
        if mdir.is_dir():
            for d in mdir.iterdir():
                if d.name not in keep_names:
                    shutil.rmtree(d, ignore_errors=True)
        runs = self.runs_dir
        if runs.is_dir():
            for d in runs.iterdir():
                if d.name not in keep_names:
                    shutil.rmtree(d, ignore_errors=True)
        return {"removed": removed, "kept": keep_id}

    def create_main(self, dataset_path: Path, size: str = "100K", name: str = "main", max_seq: int = 448, seed: int = 0,
                    replace: bool = False, confirmed: bool = False) -> dict:
        """Create THE model (random weights). If one exists it is only replaced when ``replace`` is set (its files are deleted)."""
        cur = self.active()
        if cur and not replace:
            raise LabError(f"MODEL_EXISTS: a model already exists ({cur['name']}, {cur['param_count']:,} parameters). "
                           "Replace it? Its files will be deleted.")
        if cur:
            self._plan_init(dataset_path, size, None, max_seq, "fp32", confirmed)   # refuse BEFORE deleting anything
            self.cleanup_models(None)
        entry = self.init_model(name, dataset_path, size=size, max_seq=max_seq, seed=seed, confirmed=confirmed)
        return self._set_role(entry["id"], "active")

    def train_candidate(self, dataset_path: Path, tc: TrainConfig, *, confirmed: bool = False, cancel: threading.Event | None = None,
                        on_event=None, criteria: Criteria | None = None, resume_run: str | None = None) -> dict:
        """Train a temporary candidate from the active model and compare both on the SAME dataset. Replaces nothing by itself."""
        act = self.active()
        if act is None:
            raise LabError("MODEL_MISSING: there is no model yet; create one first")
        stale = self.candidate()
        if stale and not resume_run:                    # an undecided older candidate is superseded by this run
            self._drop(stale)
        res = self.train(act["id"], dataset_path, tc, confirmed=confirmed, cancel=cancel, on_event=on_event, resume_run=resume_run)
        if res["status"] not in ("completed", "early_stopped"):
            res["candidate"] = None
            return res
        cand = self._set_role(res["model"]["id"], "candidate")
        if on_event:
            on_event({"type": "evaluating"})
        cand_res, manifest = self._measure(cand, dataset_path)
        act_res, _ = self._measure(act, dataset_path)
        baseline_for_compare = act_res if act["steps"] > 0 else None       # an untrained model is not a meaningful bar to beat
        verdict = decide(cand_res, baseline_for_compare, criteria)
        state = "eval_passed" if verdict["passed"] else "eval_failed"
        m = dict(cand["metrics"])
        m["evaluation"] = {"dataset": manifest["version"], "results": cand_res, "verdict": verdict, "compared_with": act["id"]}
        self.registry.update(cand["id"], state=state, metrics=m)
        am = dict(act["metrics"])
        am["evaluation"] = {"dataset": manifest["version"], "results": act_res}
        self.registry.update(act["id"], metrics=am)
        res["candidate"] = self.registry.get(cand["id"])
        res["active"] = self.registry.get(act["id"])
        res["verdict"] = verdict
        res["results"] = {"candidate": cand_res, "active": act_res}
        res["dataset"] = manifest["version"]
        return res

    def expand_active(self, target_params: int, seed: int = 0, confirmed: bool = False) -> dict:
        """Grow THE model into a larger one that starts from its weights. The smaller model's files are deleted (the person confirms
        this in the GUI); the larger one must be trained before it is any better. Adds capacity, not knowledge."""
        act = self.active()
        if act is None:
            raise LabError("MODEL_MISSING: there is no model to expand")
        out = self.expand(act["id"], target_params, seed=seed, confirmed=confirmed)
        new = self._set_role(out["model"]["id"], "active")
        cand = self.candidate()
        if cand:
            self._drop(cand)
        self._drop(act)
        self._forget_runs(act["name"])
        out["model"] = new
        return out

    def accept_candidate(self) -> dict:
        """The person approved the candidate: it becomes THE model and the previous model's files are deleted."""
        cand, old = self.candidate(), self.active()
        if cand is None:
            raise LabError("NO_CANDIDATE: there is no trained candidate to accept")
        if cand["state"] != "eval_passed":
            raise LabError("CANDIDATE_NOT_ACCEPTED: only a candidate that passed evaluation can replace the model")
        self._set_role(cand["id"], "active")
        if old:
            self._drop(old)
        self.registry.set_best(cand["id"])
        self._forget_runs(cand["name"])
        return self.registry.get(cand["id"])

    def reject_candidate(self) -> dict:
        cand = self.candidate()
        if cand is None:
            raise LabError("NO_CANDIDATE: there is no candidate")
        self._drop(cand)
        self._forget_runs(cand["name"])
        return {"discarded": cand["id"], "active": (self.active() or {}).get("id")}

    # ---- learning from the person's own generations ----------------------------------------------------
    def build_experience_dataset(self, store=None, *, min_labelled: int = 60, min_per_class: int = 15, name: str = "my-generations",
                                 keep_versions: int = 2) -> dict:
        """Dataset from speech the person generated and rechecked (needs their consent; see speech_engine/experience.py).

        The newest 15% (by time) form the held-out regression set: the model is judged on documents newer than it trained on.
        """
        from speech_engine.experience import ExperienceStore
        store = store or ExperienceStore()
        st = store.stats()
        if st["yes"] < min_per_class or st["no"] < min_per_class or st["labelled"] < min_labelled:
            raise LabError(f"NOT_ENOUGH_DATA: {st['labelled']} rechecked segments stored ({st['yes']} with issues, {st['no']} verified); "
                           f"need at least {min_labelled} with {min_per_class} of each outcome. Generate speech and run Recheck Speech "
                           f"to create more. ({st['unlabelled']} stored segments are still waiting for a recheck.)")
        records, _ = import_experience_records(store.labelled())
        records.sort(key=lambda r: r["created"])
        n_reg = max(5, int(len(records) * 0.15))
        main, newest = records[:-n_reg], records[-n_reg:]
        manifest = build_dataset(self.datasets_dir, name, main, NEEDS_REVIEW_LABELS, "needs_review", seed=1, regression=newest,
                                 provenance=("USER GENERATIONS: text of speech the person generated on this computer, labelled by the "
                                             "ASR/phoneme recheck (yes = issues found or failed, no = every automated check passed). "
                                             "Labels are imperfect (the recogniser can be wrong). Newest 15% held out as the regression set. "
                                             "Never uploaded."))
        self.prune_datasets(name, keep_versions)
        return manifest

    def prune_datasets(self, name: str, keep: int = 2) -> list[str]:
        """Keep only the newest ``keep`` versions of a dataset (the data is regenerated from the experience log)."""
        versions = sorted((d for d in self.datasets() if d["name"] == name), key=lambda d: d["version"])
        removed = []
        for d in versions[:-keep] if keep > 0 else versions:
            shutil.rmtree(d["path"], ignore_errors=True)
            removed.append(d["version"])
        return removed

    def learn_from_generations(self, tc: TrainConfig, *, store=None, min_labelled: int = 60, min_per_class: int = 15,
                               confirmed: bool = False, cancel: threading.Event | None = None, on_event=None) -> dict:
        """Build a dataset from the person's rechecked generations and train a candidate on it."""
        manifest = self.build_experience_dataset(store, min_labelled=min_labelled, min_per_class=min_per_class)
        res = self.train_candidate(Path(manifest["path"]), tc, confirmed=confirmed, cancel=cancel, on_event=on_event)
        res["experience_dataset"] = {k: manifest[k] for k in ("version", "counts", "label_distribution", "provenance")}
        return res
