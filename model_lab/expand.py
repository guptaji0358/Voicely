"""Explicit, validated architecture expansion.

Expansion creates a NEW, larger model that starts from the old weights. It does not create knowledge: the expanded model must be
trained further and evaluated before it can be trusted. The source checkpoint is never modified.

What is preserved (the new model computes EXACTLY the same logits on the same input, up to float rounding):
  * more layers      - new blocks are residual identities (their output projections start at zero)
  * wider feed-forward - new hidden units have zero outgoing weights
  * more attention heads - new heads have zero output-projection columns
  * longer max_seq   - new positions are appended; inputs no longer than the old max_seq are unchanged
What is NOT preserved:
  * larger d_model   - LayerNorm statistics change when channels are added, so behaviour shifts. Weights are copied into the leading
                        slice, the report records the MEASURED logit difference, and nothing claims preservation.
Not supported (reported as errors): shrinking anything, changing vocab_size or head_dim.

Optimizer state: shapes change, so the optimiser state is NOT carried over; the expanded checkpoint has none and training restarts
AdamW moments from zero (a short warm-up is recommended).
"""

from __future__ import annotations

import math

import torch

from .config import ModelConfig, config_for_target
from .model import build_model


class ExpansionError(Exception):
    pass


def check_compatible(old: ModelConfig, new: ModelConfig) -> None:
    if new.vocab_size != old.vocab_size:
        raise ExpansionError("vocab_size cannot change in an expansion (the tokenizer would no longer match)")
    if new.head_dim != old.head_dim:
        raise ExpansionError("head_dim cannot change; grow n_head instead")
    for k in ("n_layer", "d_model", "n_head", "d_ff", "max_seq"):
        if getattr(new, k) < getattr(old, k):
            raise ExpansionError(f"{k} would shrink ({getattr(old, k)} -> {getattr(new, k)}); expansion only grows models")
    if new == old:
        raise ExpansionError("the target architecture equals the source; nothing to expand")


def plan_to_target(old: ModelConfig, target_params: int) -> ModelConfig:
    """Pick a grown architecture close to ``target_params`` that every dimension of ``old`` fits inside."""
    if target_params <= old.param_count():
        raise ExpansionError(f"target {target_params:,} must exceed the source size {old.param_count():,}")
    goal = config_for_target(target_params, old.vocab_size, max(old.max_seq, 1), old.precision)
    new = ModelConfig(old.vocab_size, max(old.n_layer, goal.n_layer), max(old.d_model, goal.d_model),
                      max(old.n_head, round(goal.attn_dim / old.head_dim)), old.head_dim, max(old.d_ff, goal.d_ff),
                      old.max_seq, old.precision, old.dropout)
    check_compatible(old, new)
    return new


def _pad2(old: torch.Tensor, shape, fill=None, std: float = 0.0, gen=None) -> torch.Tensor:
    """New tensor of ``shape`` whose leading slice is ``old``; the rest is ``fill`` or small random numbers."""
    out = torch.zeros(shape) if fill is None else torch.full(shape, float(fill))
    if fill is None and std > 0:
        out = torch.randn(shape, generator=gen) * std
    sl = tuple(slice(0, s) for s in old.shape)
    out[sl] = old
    return out


def expand_state(state: dict, old: ModelConfig, new: ModelConfig, seed: int = 0, init_std: float = 0.02) -> tuple[dict, dict]:
    """Return (new_state_dict, report). Every tensor is recorded as copied / expanded / identity-initialised / randomly initialised."""
    check_compatible(old, new)
    g = torch.Generator().manual_seed(seed)
    out: dict[str, torch.Tensor] = {}
    log: list[dict] = []

    def put(name, t, action):
        old_shape = list(state[name].shape) if name in state else None
        out[name] = t
        log.append({"tensor": name, "action": action, "old_shape": old_shape, "new_shape": list(t.shape)})

    d0, d1, a0, a1, f0, f1 = old.d_model, new.d_model, old.attn_dim, new.attn_dim, old.d_ff, new.d_ff
    scale = init_std / math.sqrt(2 * new.n_layer)

    for name in ("tok.weight",):
        w = state[name]
        put(name, w.clone() if d1 == d0 else _pad2(w, (new.vocab_size, d1)), "copied" if d1 == d0 else "expanded (new channels = 0)")
    pw = state["pos.weight"]
    if new.max_seq == old.max_seq and d1 == d0:
        put("pos.weight", pw.clone(), "copied")
    else:
        put("pos.weight", _pad2(pw, (new.max_seq, d1), std=init_std, gen=g) if new.max_seq > old.max_seq else _pad2(pw, (new.max_seq, d1)),
            "expanded (new positions/channels initialised)")

    for i in range(new.n_layer):
        p = f"blocks.{i}."
        if i < old.n_layer:
            for ln in ("ln1", "ln2"):
                put(p + ln + ".weight", _pad2(state[p + ln + ".weight"], (d1,), fill=1.0), "copied" if d1 == d0 else "expanded (new gain = 1)")
                put(p + ln + ".bias", _pad2(state[p + ln + ".bias"], (d1,)), "copied" if d1 == d0 else "expanded (new bias = 0)")
            # attention: q, k, v blocks are head-major inside the 3*A rows
            qw, qb = state[p + "qkv.weight"], state[p + "qkv.bias"]
            nw, nb = torch.randn(3 * a1, d1, generator=g) * init_std, torch.zeros(3 * a1)
            for s in range(3):
                nw[s * a1:s * a1 + a0, :d0] = qw[s * a0:(s + 1) * a0]
                nw[s * a1:s * a1 + a0, d0:] = 0.0         # old heads never read the new channels
                nb[s * a1:s * a1 + a0] = qb[s * a0:(s + 1) * a0]
            put(p + "qkv.weight", nw, "copied" if (a1, d1) == (a0, d0) else "expanded (new heads/channels random, old slice copied)")
            put(p + "qkv.bias", nb, "copied" if a1 == a0 else "expanded (new = 0)")
            ow = torch.zeros(d1, a1)
            ow[:d0, :a0] = state[p + "proj.weight"]
            if d1 > d0:
                ow[d0:, :a0] = torch.randn(d1 - d0, a0, generator=g) * scale
            put(p + "proj.weight", ow, "copied" if (d1, a1) == (d0, a0) else "expanded (new heads add zero; new channels small random)")
            put(p + "proj.bias", _pad2(state[p + "proj.bias"], (d1,)), "copied" if d1 == d0 else "expanded (new = 0)")
            fw = torch.randn(f1, d1, generator=g) * init_std
            fw[:f0, :d0] = state[p + "fc.weight"]
            fw[:f0, d0:] = 0.0
            put(p + "fc.weight", fw, "copied" if (f1, d1) == (f0, d0) else "expanded (new units random, old slice copied)")
            put(p + "fc.bias", _pad2(state[p + "fc.bias"], (f1,)), "copied" if f1 == f0 else "expanded (new = 0)")
            uw = torch.zeros(d1, f1)
            uw[:d0, :f0] = state[p + "out.weight"]
            if d1 > d0:
                uw[d0:, :f0] = torch.randn(d1 - d0, f0, generator=g) * scale
            put(p + "out.weight", uw, "copied" if (d1, f1) == (d0, f0) else "expanded (new units add zero; new channels small random)")
            put(p + "out.bias", _pad2(state[p + "out.bias"], (d1,)), "copied" if d1 == d0 else "expanded (new = 0)")
        else:   # brand-new block: random input side, ZERO output side => residual identity
            put(p + "ln1.weight", torch.ones(d1), "identity-initialised")
            put(p + "ln1.bias", torch.zeros(d1), "identity-initialised")
            put(p + "qkv.weight", torch.randn(3 * a1, d1, generator=g) * init_std, "randomly initialised")
            put(p + "qkv.bias", torch.zeros(3 * a1), "randomly initialised")
            put(p + "proj.weight", torch.zeros(d1, a1), "identity-initialised (zero output)")
            put(p + "proj.bias", torch.zeros(d1), "identity-initialised (zero output)")
            put(p + "ln2.weight", torch.ones(d1), "identity-initialised")
            put(p + "ln2.bias", torch.zeros(d1), "identity-initialised")
            put(p + "fc.weight", torch.randn(f1, d1, generator=g) * init_std, "randomly initialised")
            put(p + "fc.bias", torch.zeros(f1), "randomly initialised")
            put(p + "out.weight", torch.zeros(d1, f1), "identity-initialised (zero output)")
            put(p + "out.bias", torch.zeros(d1), "identity-initialised (zero output)")
    put("ln_f.weight", _pad2(state["ln_f.weight"], (d1,), fill=1.0), "copied" if d1 == d0 else "expanded (new gain = 1)")
    put("ln_f.bias", _pad2(state["ln_f.bias"], (d1,)), "copied" if d1 == d0 else "expanded (new = 0)")

    actions = {}
    for r in log:
        k = r["action"].split(" ")[0]
        actions[k] = actions.get(k, 0) + 1
    preserving = new.d_model == old.d_model
    report = {"old_config": old.to_dict(), "new_config": new.to_dict(), "old_params": old.param_count(), "new_params": new.param_count(),
              "growth": {k: [getattr(old, k), getattr(new, k)] for k in ("n_layer", "d_model", "n_head", "d_ff", "max_seq")
                         if getattr(old, k) != getattr(new, k)},
              "expected_function_preserving": preserving,
              "preservation_note": ("exact up to rounding for inputs within the old max_seq" if preserving else
                                    "NOT preserved: d_model changed (LayerNorm statistics shift); see measured divergence"),
              "tensor_actions": actions, "tensors": log, "seed": seed,
              "optimizer": "not carried over (parameter shapes changed); training restarts the optimiser moments from zero",
              "requires_training": True,
              "note": "Expansion adds capacity, not knowledge. The expanded model must be trained and evaluated before use."}
    return out, report


def measure_preservation(old_model, new_model, samples: int = 8, seq: int | None = None, seed: int = 0) -> dict:
    """Run both models on the same random token sequences and report the actual difference (nothing is assumed)."""
    g = torch.Generator().manual_seed(seed)
    T = min(seq or old_model.cfg.max_seq, old_model.cfg.max_seq)
    x = torch.randint(4, old_model.cfg.vocab_size, (samples, T), generator=g)
    old_model.eval(); new_model.eval()
    with torch.no_grad():
        a, b = old_model(x)[0], new_model(x)[0]
    return {"max_abs_logit_diff": float((a - b).abs().max()), "top1_agreement": float((a.argmax(-1) == b.argmax(-1)).float().mean()),
            "samples": samples, "seq_len": T}


def expand_checkpoint(ck: dict, target_params: int | None = None, new_cfg: ModelConfig | None = None, seed: int = 0):
    """Validate the source checkpoint, expand, verify the result, and return (new_model, new_cfg, report). Nothing is written."""
    from .checkpoint import model_from_checkpoint
    old_model, old_cfg = model_from_checkpoint(ck)
    if new_cfg is None:
        if target_params is None:
            raise ExpansionError("give target_params or an explicit new configuration")
        new_cfg = plan_to_target(old_cfg, target_params)
    state, report = expand_state(ck["model"], old_cfg, new_cfg, seed=seed)
    new_model = build_model(new_cfg, seed=seed)
    new_model.load_state_dict(state)
    if new_model.num_params() != new_cfg.param_count():
        raise ExpansionError("internal error: expanded parameter count does not match its configuration")
    report["measured"] = measure_preservation(old_model, new_model, seq=min(old_cfg.max_seq, 64))
    report["requested_target_params"] = target_params
    return new_model, new_cfg, report
