"""Resumable, cancellable, memory-aware training loop for the next-token objective.

What "trained" means here: the loss was computed on real batches, gradients were finite and non-zero, the weights actually
changed, and validation was measured on held-out data. The first optimiser step is checked explicitly (``gradient_check``).
"""

from __future__ import annotations

import json
import math
import random
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch

from . import memory
from .checkpoint import load_checkpoint, save_checkpoint
from .config import ModelConfig
from .data import TokenSet
from .evaluate import eval_loss


class TrainingError(Exception):
    pass


@dataclass
class TrainConfig:
    steps: int = 1000                 # optimiser steps (each = grad_accum micro-batches)
    batch_size: int = 32
    grad_accum: int = 1
    lr: float = 3e-3
    min_lr_ratio: float = 0.1
    warmup_steps: int = 50
    weight_decay: float = 0.01
    grad_clip: float = 1.0
    eval_every: int = 100
    ckpt_every: int = 200
    log_every: int = 20
    early_stop_patience: int = 0      # evals without validation improvement; 0 = off
    seed: int = 1234
    label_only: bool = True           # loss on the label tokens only (the task), not on the input text
    device: str = "cpu"
    max_loss: float = 1e4             # above this the run is declared unstable
    keep_last: int = 3

    def validate(self):
        for k in ("steps", "batch_size", "grad_accum", "eval_every", "ckpt_every", "log_every"):
            if not isinstance(getattr(self, k), int) or getattr(self, k) < 1:
                raise TrainingError(f"{k} must be a positive integer")
        if not (self.lr > 0 and math.isfinite(self.lr)):
            raise TrainingError("lr must be positive and finite")


def lr_at(step: int, c: TrainConfig) -> float:
    if step < c.warmup_steps:
        return c.lr * (step + 1) / max(1, c.warmup_steps)
    prog = (step - c.warmup_steps) / max(1, c.steps - c.warmup_steps)
    return c.lr * (c.min_lr_ratio + (1 - c.min_lr_ratio) * 0.5 * (1 + math.cos(math.pi * min(1.0, prog))))


def _rng_state():
    return {"torch": torch.get_rng_state(), "python": random.getstate()}


class Trainer:
    def __init__(self, model, cfg: ModelConfig, tc: TrainConfig, train: TokenSet, val: TokenSet, run_dir: Path,
                 meta: dict | None = None):
        tc.validate()
        self.model, self.cfg, self.tc, self.train, self.val = model, cfg, tc, train, val
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.meta = meta or {}
        self.dev = torch.device(tc.device)
        if self.dev.type == "cuda" and not torch.cuda.is_available():
            raise TrainingError("CUDA requested but not available; use device='cpu'")
        self.precision_effective = cfg.precision if (self.dev.type == "cuda" and cfg.precision != "fp32") else "fp32"
        self.model.to(self.dev)
        decay = [p for n, p in model.named_parameters() if p.dim() >= 2]
        nodecay = [p for n, p in model.named_parameters() if p.dim() < 2]
        self.opt = torch.optim.AdamW([{"params": decay, "weight_decay": tc.weight_decay}, {"params": nodecay, "weight_decay": 0.0}],
                                     lr=tc.lr, betas=(0.9, 0.95))
        self.rng = np.random.default_rng(tc.seed)
        self.step = 0
        self.best_val = math.inf
        self.stale_evals = 0
        self.history: list[dict] = []
        self.gradient_check: dict | None = None

    # ---- resume -----------------------------------------------------------------------------------
    def resume(self, path: Path) -> None:
        ck = load_checkpoint(path)
        if ck["config"] != self.cfg.to_dict():
            raise TrainingError("cannot resume: checkpoint architecture differs from the model being trained")
        if "optimizer" not in ck or "trainer" not in ck:
            raise TrainingError("this checkpoint has no optimiser/trainer state (it is a model-only checkpoint); start a new run from it instead")
        self.model.load_state_dict(ck["model"])
        self.opt.load_state_dict(ck["optimizer"])
        t = ck["trainer"]
        self.step, self.best_val, self.stale_evals = t["step"], t["best_val"], t["stale_evals"]
        self.rng.bit_generator.state = t["np_rng"]
        torch.set_rng_state(t["torch_rng"])
        random.setstate(t["py_rng"])
        self.history = t.get("history", [])
        self.gradient_check = t.get("gradient_check")

    def _trainer_state(self) -> dict:
        return {"step": self.step, "best_val": self.best_val, "stale_evals": self.stale_evals, "np_rng": self.rng.bit_generator.state,
                "torch_rng": torch.get_rng_state(), "py_rng": random.getstate(), "history": self.history[-2000:],
                "gradient_check": self.gradient_check, "train_config": asdict(self.tc)}

    def save(self, name: str, overwrite: bool = False) -> dict:
        return save_checkpoint(self.run_dir / name, self.model, self.cfg, optimizer=self.opt, trainer_state=self._trainer_state(),
                               meta={**self.meta, "step": self.step}, overwrite=overwrite)

    def _prune(self) -> None:
        cks = sorted(self.run_dir.glob("step*.pt"))
        for p in cks[:-self.tc.keep_last]:
            try:
                p.unlink()
            except OSError:
                pass

    # ---- one optimiser step -----------------------------------------------------------------------
    def _micro(self):
        idx = self.rng.choice(len(self.train), size=min(self.tc.batch_size, len(self.train)), replace=False)
        x, y = self.train.batch(idx, self.tc.label_only)
        return x.to(self.dev), y.to(self.dev)

    def _train_step(self) -> tuple[float, float]:
        c = self.tc
        for g in self.opt.param_groups:
            g["lr"] = lr_at(self.step, c)
        self.model.train()
        self.opt.zero_grad(set_to_none=True)
        total = 0.0
        for _ in range(c.grad_accum):
            x, y = self._micro()
            if self.precision_effective != "fp32":
                dt = torch.bfloat16 if self.precision_effective == "bf16" else torch.float16
                with torch.autocast(self.dev.type, dtype=dt):
                    _, loss = self.model(x, y)
            else:
                _, loss = self.model(x, y)
            if not torch.isfinite(loss):
                raise FloatingPointError(f"non-finite loss {float(loss.detach())} at step {self.step}")
            (loss / c.grad_accum).backward()
            total += float(loss.detach()) / c.grad_accum
        gn = float(torch.nn.utils.clip_grad_norm_(self.model.parameters(), c.grad_clip))
        if not math.isfinite(gn):
            raise FloatingPointError(f"non-finite gradient norm at step {self.step}")
        first = self.gradient_check is None
        before = [p.detach().clone() for p in self.model.parameters()] if first else None
        self.opt.step()
        if first:
            delta = max(float((p.detach() - b).abs().max()) for p, b in zip(self.model.parameters(), before))
            n_with_grad = sum(1 for p in self.model.parameters() if p.grad is not None and float(p.grad.abs().sum()) > 0)
            self.gradient_check = {"grad_norm": gn, "max_weight_change": delta, "tensors_with_gradient": n_with_grad,
                                   "tensors_total": len(list(self.model.parameters())), "loss": total}
            if gn <= 0 or delta <= 0 or n_with_grad == 0:
                raise TrainingError(f"first step did not change the weights (grad_norm={gn}, max_change={delta}); training is not working")
        return total, gn

    # ---- run --------------------------------------------------------------------------------------
    def run(self, cancel: threading.Event | None = None, on_event=None) -> dict:
        """Train until tc.steps. Returns a result dict; status is completed | cancelled | early_stopped | diverged."""
        cancel = cancel or threading.Event()
        emit = on_event or (lambda e: None)
        t0, start_step = time.time(), self.step
        mem0 = memory.current_rss()
        status, error = "completed", None
        recent: list[float] = []
        log = open(self.run_dir / "metrics.jsonl", "a", encoding="utf-8")
        try:
            while self.step < self.tc.steps:
                if cancel.is_set():
                    status = "cancelled"
                    break
                try:
                    loss, gn = self._train_step()
                except FloatingPointError as exc:
                    status, error = "diverged", str(exc)
                    break
                if loss > self.tc.max_loss:
                    status, error = "diverged", f"loss {loss:.3g} exceeded max_loss at step {self.step}"
                    break
                self.step += 1
                recent = (recent + [loss])[-20:]
                if self.step % self.tc.log_every == 0 or self.step == self.tc.steps:
                    rec = {"type": "train", "step": self.step, "loss": loss, "grad_norm": gn,
                           "lr": self.opt.param_groups[0]["lr"], "elapsed": time.time() - t0, "rss": memory.current_rss()}
                    log.write(json.dumps(rec) + "\n"); log.flush()
                    emit(rec)
                if self.step % self.tc.eval_every == 0 or self.step == self.tc.steps:
                    ev = eval_loss(self.model, self.val, self.tc.label_only)
                    rec = {"type": "val", "step": self.step, "loss": ev["loss"], "perplexity": ev["perplexity"],
                           "train_loss": sum(recent) / len(recent)}
                    self.history.append(rec)
                    log.write(json.dumps(rec) + "\n"); log.flush()
                    emit(rec)
                    if ev["loss"] < self.best_val - 1e-6:
                        self.best_val, self.stale_evals = ev["loss"], 0
                    else:
                        self.stale_evals += 1
                        if self.tc.early_stop_patience and self.stale_evals >= self.tc.early_stop_patience:
                            status = "early_stopped"
                            break
                if self.step % self.tc.ckpt_every == 0:
                    self.save(f"step{self.step:08d}.pt")
                    self.save("latest.pt", overwrite=True)
                    self._prune()
        finally:
            log.close()
        info = None
        if status != "diverged":                      # a diverged state is never written as the resumable "latest"
            info = self.save("latest.pt", overwrite=True)
        res = {"status": status, "error": error, "steps_done": self.step, "steps_this_run": self.step - start_step,
               "target_steps": self.tc.steps, "best_val_loss": self.best_val if math.isfinite(self.best_val) else None,
               "final_train_loss": sum(recent) / len(recent) if recent else None, "elapsed_s": time.time() - t0,
               "gradient_check": self.gradient_check, "device": str(self.dev), "precision_effective": self.precision_effective,
               "peak_rss_bytes": memory.peak_rss(), "rss_growth_bytes": max(0, memory.current_rss() - mem0), "latest": info,
               "train_config": asdict(self.tc)}
        emit({"type": "done", **{k: v for k, v in res.items() if k != "train_config"}})
        return res
