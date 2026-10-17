"""Reproducible experiment: train several sizes from scratch, evaluate, and test an expansion. Writes a JSON report.

    python -m model_lab.experiments --root <lab folder> --out report.json

Everything is real: random init, real training steps on the synthetic dataset, real evaluation, measured peak memory.
Sizes that the memory guard refuses are reported as refused (never silently skipped or faked).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import memory
from .config import SIZE_TARGETS, preset
from .lab import Lab, LabError
from .train import TrainConfig


def run(root: Path, sizes=("1K", "10K", "100K", "1M"), steps: int = 1500, n_per_label: int = 500, expand_to: int = 1_000_000,
        expand_steps: int = 600) -> dict:
    lab = Lab(root)
    ds = lab.make_synthetic_dataset("review-category", n_per_label)
    report = {"hardware": memory.hardware_report(), "dataset": {k: ds[k] for k in ("version", "counts", "provenance", "label_distribution", "cleaning")},
              "runs": {}, "started": time.time()}
    ids = {}
    for size in sizes:
        r = {}
        try:
            entry = lab.init_model(f"exp-{size}", ds["path"], size=size, seed=11, max_seq=96)
            r["init"] = {"params": entry["param_count"], "config": entry["config"], "seed": entry["seed"], "state": entry["state"]}
            ev0 = lab.evaluate(entry["id"], ds["path"], limit=300)
            r["random_init_eval"] = {s: {k: ev0["results"][s][k] for k in ("accuracy", "loss")} for s in ("val", "regression")}
            res = lab.train(entry["id"], ds["path"], TrainConfig(steps=steps, batch_size=32, eval_every=max(50, steps // 10),
                                                                 ckpt_every=max(100, steps // 5), warmup_steps=min(100, steps // 10),
                                                                 lr=3e-3 if size in ("1K", "10K", "100K") else 1.5e-3))
            r["train"] = {k: res[k] for k in ("status", "steps_done", "final_train_loss", "best_val_loss", "elapsed_s", "peak_rss_bytes",
                                                "estimated_peak_bytes", "device", "gradient_check")}
            ev = lab.evaluate(res["model"]["id"], ds["path"])
            r["eval"] = {s: {k: ev["results"][s][k] for k in ("accuracy", "macro_f1", "loss", "perplexity", "majority_rate", "baseline_accuracy")}
                         for s in ("train", "val", "test", "regression")}
            r["verdict"] = ev["verdict"]
            r["state"] = ev["state"]
            ids[size] = res["model"]["id"]
        except LabError as exc:
            r["refused_or_failed"] = str(exc)
        report["runs"][size] = r
    # expansion test: smallest trained model that is smaller than the target
    src_size = next((s for s in ("100K", "10K") if s in ids), None)
    if src_size:
        x = {"source": src_size}
        try:
            ex = lab.expand(ids[src_size], expand_to, seed=3)
            x["report"] = {k: ex["report"][k] for k in ("old_params", "new_params", "growth", "expected_function_preserving", "tensor_actions",
                                                          "measured", "optimizer")}
            ev_before = lab.evaluate(ex["model"]["id"], ds["path"], limit=300)
            x["eval_before_training"] = {"state": ev_before["state"], "val_accuracy": ev_before["results"]["val"]["accuracy"],
                                         "checks": [c for c in ev_before["verdict"]["checks"] if not c["ok"]]}
            res = lab.train(ex["model"]["id"], ds["path"], TrainConfig(steps=expand_steps, batch_size=32, eval_every=max(50, expand_steps // 6),
                                                                      ckpt_every=expand_steps, warmup_steps=60, lr=1e-3))
            ev = lab.evaluate(res["model"]["id"], ds["path"])
            x["after_training"] = {"steps": res["steps_this_run"], "state": ev["state"], "peak_rss_bytes": res["peak_rss_bytes"],
                                   "val_accuracy": ev["results"]["val"]["accuracy"], "regression_accuracy": ev["results"]["regression"]["accuracy"]}
        except LabError as exc:
            x["refused_or_failed"] = str(exc)
        report["expansion"] = x
    # sizes that are only architecture targets here
    report["targets_only"] = {}
    for size in ("100M", "1B", "8B"):
        cfg = preset(size, 80, 96)
        est = memory.estimate_training_bytes(cfg, 8)
        report["targets_only"][size] = {"params": cfg.param_count(), "estimated_training_bytes": est["total"],
                                       "decision": memory.check_budget(est["total"]).to_dict()}
    report["finished"] = time.time()
    return report


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--steps", type=int, default=1500)
    ap.add_argument("--sizes", default="1K,10K,100K,1M")
    a = ap.parse_args()
    rep = run(Path(a.root), tuple(a.sizes.split(",")), a.steps)
    Path(a.out).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    print("wrote", a.out)
