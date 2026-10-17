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
from .data import SEP, TokenSet, build_dataset, load_dataset, list_datasets, tokenize_split, SPLITS
from .evaluate import Criteria, decide, evaluate_split
from .expand import expand_checkpoint
from .registry import Registry, RegistryError, lab_dir
from .tokenizer import Tokenizer
from .train import Trainer, TrainConfig, TrainingError


class LabError(Exception):
    pass


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
    def model_dir(self, name: str) -> Path:
        return self.registry.checkpoint_dir(name)

    def tokenizer_for(self, entry: dict) -> Tokenizer:
        return Tokenizer.load(self.model_dir(entry["name"]) / "tokenizer.json")

    def init_model(self, name: str, dataset_path: Path, size: str | None = "100K", cfg: ModelConfig | None = None,
                   max_seq: int = 96, seed: int = 0, precision: str = "fp32", confirmed: bool = False) -> dict:
        """Create a randomly initialised model + tokenizer learned from the dataset's TRAIN split. Loads nothing pretrained."""
        from .model import build_model
        manifest, splits = load_dataset(dataset_path)
        tok = Tokenizer.train([r["text"] for r in splits["train"]] + manifest["labels"] + [SEP])
        cfg = cfg or preset(size, tok.size, max_seq, precision)
        if cfg.vocab_size != tok.size:
            raise LabError(f"config vocab_size {cfg.vocab_size} does not match the tokenizer ({tok.size})")
        est = memory.estimate_training_bytes(cfg, 32)
        dec = memory.check_budget(est["total"], confirmed=confirmed)
        if dec.verdict != "ok":
            raise LabError(f"cannot instantiate {cfg.param_count():,} parameters: {dec.reason}")
        model = build_model(cfg, seed)
        d = self.model_dir(name)
        if not (d / "tokenizer.json").exists():
            tok.save(d / "tokenizer.json")
        else:
            tok = Tokenizer.load(d / "tokenizer.json")
            if tok.size != cfg.vocab_size:
                raise LabError("an existing model with this name uses a different tokenizer; choose another name")
        version = len([m for m in self.registry.models() if m["name"] == name]) + 1
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
        est = memory.estimate_training_bytes(cfg, tc.batch_size * 1)
        dec = memory.check_budget(est["total"], confirmed=confirmed)
        if dec.verdict != "ok":
            raise LabError(("CONFIRMATION_NEEDED: " if dec.verdict == "needs_confirmation" else "REFUSED: ") + dec.reason)
        ck = load_checkpoint(parent["checkpoint"], parent["sha256"])
        model, _ = model_from_checkpoint(ck)
        tok = self.tokenizer_for(parent)
        manifest, _ = load_dataset(dataset_path)
        tag = self.prepare_tokens(dataset_path, tok, cfg.max_seq)
        train, val = TokenSet(dataset_path, "train", tag), TokenSet(dataset_path, "val", tag)
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
            version = len([m for m in self.registry.models() if m["name"] == parent["name"]]) + 1
            steps_total = parent["steps"] + result["steps_this_run"]
            info = save_checkpoint(d / f"v{version}-step{steps_total}.pt", model, cfg,
                                   meta={"origin": "scratch", "parent": model_id, "dataset_version": manifest["version"],
                                         "train_config": asdict(tc), "result": {k: v for k, v in result.items() if k != "train_config"}})
            notes = dict(parent.get("notes") or {})
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
    def evaluate(self, model_id: str, dataset_path: Path, criteria: Criteria | None = None, limit: int | None = None) -> dict:
        entry = self.registry.get(model_id)
        ck = load_checkpoint(entry["checkpoint"], entry["sha256"])
        model, cfg = model_from_checkpoint(ck)
        tok = self.tokenizer_for(entry)
        manifest, splits = load_dataset(dataset_path)
        tag = self.prepare_tokens(dataset_path, tok, cfg.max_seq)
        labels = manifest["labels"]
        res = {}
        for s, lim in (("train", 600), ("val", limit), ("test", limit), ("regression", limit)):
            res[s] = evaluate_split(model, tok, TokenSet(dataset_path, s, tag), splits[s], labels, limit=lim)
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
        version = len([m for m in self.registry.models() if m["name"] == src["name"]]) + 1
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
