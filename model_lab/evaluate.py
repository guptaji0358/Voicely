"""Evaluation of the TASK being trained (category prediction), not just language-model loss, plus acceptance criteria.

* loss / perplexity are over the supervised (label) tokens only - they are task perplexity, NOT a speech-quality measure
* accuracy / macro-F1 come from real greedy generation of the label, compared with the trusted target
* the deterministic rule baseline and the majority-class rate are reported next to every learned result
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass

import numpy as np
import torch

from . import baseline
from .config import EOS
from .data import IGNORE, TokenSet, encode_example
from .tokenizer import Tokenizer


def _metrics(pred: list[str], gold: list[str], labels: list[str]) -> dict:
    n = len(gold)
    acc = sum(p == g for p, g in zip(pred, gold)) / n if n else 0.0
    per, f1s = {}, []
    for l in labels:
        tp = sum(p == l and g == l for p, g in zip(pred, gold))
        fp = sum(p == l and g != l for p, g in zip(pred, gold))
        fn = sum(p != l and g == l for p, g in zip(pred, gold))
        prec, rec = (tp / (tp + fp) if tp + fp else 0.0), (tp / (tp + fn) if tp + fn else 0.0)
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        support = tp + fn
        per[l] = {"precision": prec, "recall": rec, "f1": f1, "support": support}
        if support:
            f1s.append(f1)
    return {"n": n, "accuracy": acc, "macro_f1": sum(f1s) / len(f1s) if f1s else 0.0, "per_class": per}


def majority_rate(gold: list[str]) -> float:
    return max(Counter(gold).values()) / len(gold) if gold else 0.0


@torch.no_grad()
def eval_loss(model, ts: TokenSet, label_only: bool = True, batch: int = 128, limit: int | None = None) -> dict:
    was = model.training
    model.eval()
    n = len(ts) if limit is None else min(limit, len(ts))
    total, count = 0.0, 0
    for i in range(0, n, batch):
        x, y = ts.batch(np.arange(i, min(i + batch, n)), label_only)
        logits, _ = model(x)
        loss = torch.nn.functional.cross_entropy(logits.reshape(-1, logits.size(-1)), y.reshape(-1), ignore_index=IGNORE, reduction="sum")
        k = int((y != IGNORE).sum())
        total += float(loss)
        count += k
    model.train(was)
    mean = total / max(count, 1)
    return {"loss": mean, "perplexity": math.exp(min(mean, 50)), "tokens": count}


@torch.no_grad()
def predict_labels(model, tok: Tokenizer, records: list[dict], labels: list[str], limit: int | None = None) -> tuple[list[str], list[str]]:
    """Greedy generation of the label for each record. Returns (predicted, gold). Unparseable output counts as wrong."""
    longest = max(len(tok.encode(l)) for l in labels) + 1
    pred, gold = [], []
    for r in records[:limit]:
        ids, pl = encode_example(tok, r)
        out = model.generate(torch.tensor([ids[:pl]]), longest, temperature=0.0, eos_id=EOS)[0].tolist()[pl:]
        if EOS in out:
            out = out[:out.index(EOS)]
        pred.append(tok.decode(out))
        gold.append(r["target"])
    return pred, gold


def evaluate_split(model, tok: Tokenizer, ts: TokenSet, records: list[dict], labels: list[str], label_only: bool = True,
                   limit: int | None = None, task: str = "review_category") -> dict:
    recs = records[:limit] if limit else records
    out = eval_loss(model, ts, label_only, limit=len(recs))
    pred, gold = predict_labels(model, tok, recs, labels)
    out.update(_metrics(pred, gold, labels))
    out["invalid_outputs"] = sum(p not in labels for p in pred)
    out["majority_rate"] = majority_rate(gold)
    bp = [baseline.predict_for(task, r["text"]) for r in recs]
    bm = _metrics(bp, gold, labels)
    out["baseline_accuracy"], out["baseline_macro_f1"] = bm["accuracy"], bm["macro_f1"]
    return out


# ------------------------------------------------------------------------------------------ acceptance
@dataclass
class Criteria:
    min_over_majority: float = 0.10       # must beat predicting the most common class by this margin (a random model must fail)
    min_gain_over_best: float = 0.0       # validation accuracy must be >= best + this
    max_val_loss_ratio: float = 1.05      # val loss must be <= best * this
    max_regression_drop: float = 0.02     # regression accuracy may not fall more than this below the best
    max_overfit_gap: float = 0.15         # train accuracy - val accuracy
    require_beats_baseline: bool = False  # informational by default; set True to make the rule baseline a hard gate


def decide(cand: dict, best: dict | None, crit: Criteria | None = None) -> dict:
    """cand/best: {"train": metrics, "val": metrics, "regression": metrics}. Returns {"passed": bool, "checks": [...]}."""
    c = crit or Criteria()
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    v, r, t = cand["val"], cand["regression"], cand.get("train", {})
    add("finite_loss", all(math.isfinite(x["loss"]) for x in (v, r)), f"val loss {v['loss']:.4f}, regression loss {r['loss']:.4f}")
    add("beats_majority_class", v["accuracy"] >= v["majority_rate"] + c.min_over_majority,
        f"val accuracy {v['accuracy']:.3f} vs majority-class {v['majority_rate']:.3f} (+{c.min_over_majority:.2f} required)")
    if t:
        gap = t["accuracy"] - v["accuracy"]
        add("no_overfitting", gap <= c.max_overfit_gap, f"train {t['accuracy']:.3f} - val {v['accuracy']:.3f} = {gap:.3f} (max {c.max_overfit_gap})")
    add("beats_rule_baseline" if c.require_beats_baseline else "vs_rule_baseline_(info)",
        (v["accuracy"] > v["baseline_accuracy"]) or not c.require_beats_baseline,
        f"val accuracy {v['accuracy']:.3f} vs rule baseline {v['baseline_accuracy']:.3f}")
    if best:
        bv, br = best["val"], best["regression"]
        add("val_accuracy_not_worse_than_best", v["accuracy"] >= bv["accuracy"] + c.min_gain_over_best,
            f"{v['accuracy']:.3f} vs best {bv['accuracy']:.3f}")
        add("val_loss_not_worse_than_best", v["loss"] <= bv["loss"] * c.max_val_loss_ratio, f"{v['loss']:.4f} vs best {bv['loss']:.4f}")
        add("no_regression_on_heldout", r["accuracy"] >= br["accuracy"] - c.max_regression_drop,
            f"regression accuracy {r['accuracy']:.3f} vs best {br['accuracy']:.3f}")
    return {"passed": all(x["ok"] for x in checks), "checks": checks}
