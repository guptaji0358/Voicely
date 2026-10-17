"""Model Lab: datasets, training loop, evaluation/acceptance, promotion, expansion, orchestration, worker protocol."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
import torch

from model_lab import baseline, data, memory, synth
from model_lab.checkpoint import load_checkpoint, model_from_checkpoint, save_checkpoint, sha256_file
from model_lab.config import ModelConfig
from model_lab.evaluate import Criteria, _metrics, decide, eval_loss
from model_lab.expand import ExpansionError, expand_state, measure_preservation, plan_to_target
from model_lab.lab import Lab, LabError
from model_lab.model import build_model
from model_lab.registry import Registry, RegistryError
from model_lab.tokenizer import Tokenizer
from model_lab.train import TrainConfig, Trainer, TrainingError

ROOT = Path(__file__).resolve().parents[1]
LABELS = synth.LABELS


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    """A real small dataset version + tokenizer, shared by the tests in this module."""
    root = tmp_path_factory.mktemp("lab")
    lab = Lab(root)
    ds = lab.make_synthetic_dataset(n_per_label=60, seed=3)
    return lab, ds


# ---- 8/9. datasets: validation, versioning, no leakage -----------------------------------------------------------------
def test_dataset_splits_do_not_overlap_and_are_versioned(tiny):
    lab, ds = tiny
    manifest, splits = data.load_dataset(ds["path"])
    keys = {s: {data.norm_key(r["text"]) for r in rs} for s, rs in splits.items()}
    for a in keys:
        for b in keys:
            if a < b:
                assert not keys[a] & keys[b], f"{a}/{b} share text"
    assert manifest["version"].startswith("v0001-") and manifest["counts"]["train"] > manifest["counts"]["val"] > 0
    assert "SYNTHETIC" in manifest["provenance"]
    # the regression split is built from trigger words that never occur in train/val/test
    held = {w for label in ("acronym", "heteronym") for w in synth.POOLS[label]["heldout"]}
    seen = {w for s in ("train", "val", "test") for r in splits[s] for w in r["text"].replace(".", "").split()}
    assert not held & seen and held & {w for r in splits["regression"] for w in r["text"].replace(".", "").split()}
    assert all(r["source"] == "synthetic-heldout" for r in splits["regression"])
    assert sum(manifest["label_distribution"]["train"].values()) == manifest["counts"]["train"]


def test_dataset_is_immutable_checksummed_and_tamper_evident(tiny, tmp_path):
    lab, ds = tiny
    v2 = lab.make_synthetic_dataset(n_per_label=60, seed=3)           # same content: a new version number, same hash suffix
    assert v2["version"].split("-")[1] == ds["version"].split("-")[1] and v2["version"] != ds["version"]
    copy = data.export_dataset(ds["path"], tmp_path / "export")
    (Path(copy) / "val.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(data.DatasetError, match="checksum"):
        data.load_dataset(copy)
    names = [d["version"] for d in lab.datasets()]
    assert ds["version"] in names
    data.delete_dataset(v2["path"])
    assert v2["version"] not in [d["version"] for d in lab.datasets()]


def test_cleaning_dedupes_and_excludes_contradictions():
    recs = [{"text": "Call Dr. Lee.", "target": "abbreviation"}, {"text": "call  dr. lee.", "target": "abbreviation"},
            {"text": "It is 5 km", "target": "unit"}, {"text": "it is 5 km", "target": "number"},
            {"text": "", "target": "plain"}, {"text": "x", "target": "not-a-label"}, {"text": "Fine.", "target": "plain"}]
    kept, rep = data.clean_records(recs, LABELS, "review_category")
    texts = {r["text"] for r in kept}
    assert texts == {"Call Dr. Lee.", "Fine."} and rep["duplicates"] == 1
    assert rep["contradictions"][0]["targets"] == ["number", "unit"] and len(rep["invalid"]) == 2


def test_split_is_a_pure_function_of_the_text():
    assert data.split_of("Same text", 1) == data.split_of("  same   TEXT ", 1)
    counts = {s: 0 for s in ("train", "val", "test")}
    for i in range(2000):
        counts[data.split_of(f"example {i}", 1)] += 1
    assert 1500 < counts["train"] < 1700 and 120 < counts["val"] < 280


def test_feedback_import_uses_only_confirmed_mappable_corrections():
    fb = [{"id": "a", "original_text": "NASA launched.", "succeeded": True, "correction": {"category": "acronym"}, "language": "en"},
          {"id": "b", "original_text": "Unsure.", "succeeded": None, "correction": {"category": "acronym"}},
          {"id": "c", "original_text": "Smith said.", "succeeded": True, "correction": {"category": "name"}}]
    used, skipped = data.import_feedback_records(fb, LABELS)
    assert [u["target"] for u in used] == ["acronym"] and used[0]["source"] == "feedback"
    assert {s["id"] for s in skipped} == {"b", "c"}


def test_baseline_rules_and_metrics():
    assert baseline.predict("We met on March 3rd.") == "date" and baseline.predict("It weighs 5 kg") == "unit"
    assert baseline.predict("He paid $45 today") == "number" and baseline.predict("NASA said so") == "acronym"
    assert baseline.predict("Please read it") == "heteronym" and baseline.predict("A quiet garden") == "plain"
    m = _metrics(["a", "a", "b", "b"], ["a", "b", "b", "b"], ["a", "b"])
    assert m["accuracy"] == 0.75 and abs(m["per_class"]["b"]["recall"] - 2 / 3) < 1e-9 and m["per_class"]["a"]["precision"] == 0.5


# ---- 4/10. training step, validation metrics --------------------------------------------------------------------------
def _setup(tiny, tmp_path, steps=30, **tc):
    lab, ds = tiny
    manifest, splits = data.load_dataset(ds["path"])
    tok = Tokenizer.train([r["text"] for r in splits["train"]] + LABELS + [data.SEP])
    cfg = ModelConfig(tok.size, 2, 24, 2, 12, 48, 96)
    tag = lab.prepare_tokens(ds["path"], tok, 96)
    train, val = data.TokenSet(ds["path"], "train", tag), data.TokenSet(ds["path"], "val", tag)
    model = build_model(cfg, 0)
    t = Trainer(model, cfg, TrainConfig(steps=steps, batch_size=16, eval_every=10, ckpt_every=10, log_every=5, warmup_steps=3, **tc),
                train, val, tmp_path / "run")
    return t, model, cfg, tok, train, val


def test_training_step_computes_valid_loss_and_changes_weights(tiny, tmp_path):
    t, model, cfg, tok, train, val = _setup(tiny, tmp_path, steps=40)
    before = {k: v.clone() for k, v in model.state_dict().items()}
    first_loss = eval_loss(model, val)["loss"]
    res = t.run()
    assert res["status"] == "completed" and res["steps_done"] == 40
    gc = res["gradient_check"]
    assert gc["grad_norm"] > 0 and gc["max_weight_change"] > 0 and gc["tensors_with_gradient"] == gc["tensors_total"]
    assert abs(gc["loss"] - np.log(tok.size)) < 1.5                   # the first loss is the untrained cross-entropy
    changed = sum(not torch.equal(before[k], v) for k, v in model.state_dict().items())
    assert changed >= len(before) - 2
    assert eval_loss(model, val)["loss"] < first_loss                 # learning actually reduced held-out loss
    assert res["peak_rss_bytes"] > 0 and (tmp_path / "run" / "metrics.jsonl").read_text().count("\n") >= 4


def test_validation_loss_matches_manual_computation(tiny, tmp_path):
    t, model, cfg, tok, train, val = _setup(tiny, tmp_path)
    got = eval_loss(model, val, batch=7)
    x, y = val.batch(np.arange(len(val)), True)
    with torch.no_grad():
        logits = model.eval()(x)[0]
        want = torch.nn.functional.cross_entropy(logits.reshape(-1, logits.size(-1)), y.reshape(-1), ignore_index=-100)
    assert abs(got["loss"] - float(want)) < 1e-5 and abs(got["perplexity"] - np.exp(float(want))) < 1e-3


def test_label_only_masking_counts_only_the_answer_tokens(tiny, tmp_path):
    t, model, cfg, tok, train, val = _setup(tiny, tmp_path)
    x, y = val.batch(np.arange(4), True)
    x2, y2 = val.batch(np.arange(4), False)
    assert int((y != -100).sum()) < int((y2 != -100).sum())
    for i in range(4):
        n_label = len(tok.encode(val_records(tiny)[i]["target"])) + 1          # label tokens + EOS
        assert int((y[i] != -100).sum()) == n_label


def val_records(tiny):
    return data.load_dataset(tiny[1]["path"])[1]["val"]


# ---- 5/13. resume, cancel, instability -----------------------------------------------------------------------------------
def test_cancel_then_resume_reproduces_an_uninterrupted_run(tiny, tmp_path):
    a, ma, *_ = _setup(tiny, tmp_path / "a", steps=30)
    ra = a.run()
    b, mb, cfg, tok, train, val = _setup(tiny, tmp_path / "b", steps=30)
    cancel = threading.Event()
    rb = b.run(cancel, lambda e: cancel.set() if e["type"] == "train" and e["step"] >= 15 else None)
    assert rb["status"] == "cancelled" and 15 <= rb["steps_done"] < 30
    c, mc, *_ = _setup(tiny, tmp_path / "b2", steps=30)
    c.resume(tmp_path / "b" / "run" / "latest.pt")
    assert c.step == rb["steps_done"]
    rc = c.run()
    assert rc["status"] == "completed" and rc["steps_done"] == 30
    for k, v in ma.state_dict().items():
        assert torch.allclose(v, mc.state_dict()[k], atol=1e-6), k    # same data order, same optimiser state => same weights


def test_cancel_never_corrupts_existing_checkpoints(tiny, tmp_path):
    t, model, *_ = _setup(tiny, tmp_path, steps=60)
    cancel = threading.Event()
    t.run(cancel, lambda e: cancel.set() if e["type"] == "train" and e["step"] >= 25 else None)
    files = list((tmp_path / "run").glob("*.pt"))
    assert files and not list((tmp_path / "run").glob("*.part"))
    for f in files:
        model_from_checkpoint(load_checkpoint(f))                     # every checkpoint still loads and validates


def test_resume_refuses_mismatched_architecture_and_model_only_checkpoints(tiny, tmp_path):
    t, model, cfg, *_ = _setup(tiny, tmp_path, steps=10)
    t.run()
    other_t, *_ = _setup(tiny, tmp_path / "other", steps=10)
    save_checkpoint(tmp_path / "model_only.pt", model, cfg, meta={})
    with pytest.raises(TrainingError, match="no optimiser"):
        other_t.resume(tmp_path / "model_only.pt")


def test_nonfinite_loss_stops_the_run_without_overwriting_latest(tiny, tmp_path):
    t, model, *_ = _setup(tiny, tmp_path, steps=40)
    good = t.run()                                                    # produces a valid latest.pt
    latest = tmp_path / "run" / "latest.pt"
    before = sha256_file(latest)
    t2, m2, *_ = _setup(tiny, tmp_path / "second", steps=40)
    t2.run_dir = tmp_path / "run"                                     # same folder: must not be clobbered by a diverged run
    with torch.no_grad():
        m2.tok.weight[:, 0] = float("nan")
    res = t2.run()
    assert res["status"] == "diverged" and "non-finite" in res["error"]
    assert sha256_file(latest) == before and good["status"] == "completed"


def test_early_stopping_and_validation_of_config(tiny, tmp_path):
    t, *_ = _setup(tiny, tmp_path, steps=400, early_stop_patience=2)
    t.tc.lr = 1e-9                                                    # nothing improves
    res = t.run()
    assert res["status"] == "early_stopped" and res["steps_done"] < 400
    with pytest.raises(TrainingError):
        TrainConfig(steps=0).validate()


# ---- 11. acceptance gates and promotion ---------------------------------------------------------------------------------
def _metrics_blob(acc, loss, maj=0.2, base=0.5):
    m = {"accuracy": acc, "loss": loss, "majority_rate": maj, "baseline_accuracy": base}
    return {"train": {**m, "accuracy": acc + 0.02}, "val": m, "regression": {**m}}


def test_decide_rejects_random_regressions_and_overfitting():
    assert not decide(_metrics_blob(0.16, 1.9), None)["passed"]                                   # chance level
    assert decide(_metrics_blob(0.8, 0.5), None)["passed"]
    best = _metrics_blob(0.8, 0.5)
    assert not decide(_metrics_blob(0.75, 0.5), best)["passed"]                                   # lower accuracy than best
    assert not decide(_metrics_blob(0.82, 0.9), best)["passed"]                                   # loss regression
    worse_reg = _metrics_blob(0.85, 0.4)
    worse_reg["regression"]["accuracy"] = 0.5
    assert not decide(worse_reg, best)["passed"]                                                  # held-out regression drop
    over = _metrics_blob(0.7, 0.5)
    over["train"]["accuracy"] = 1.0
    assert not decide(over, None)["passed"]                                                       # overfitting gap
    nan = _metrics_blob(0.9, float("nan"))
    assert not decide(nan, None)["passed"]


def test_registry_state_machine_requires_passed_evaluation_and_approval(tmp_path):
    reg = Registry(tmp_path)
    cfg = ModelConfig(40, 1, 8, 1, 8, 16, 16).to_dict()
    mk = lambda state: reg.register(name="m", config=cfg, param_count=1, checkpoint="x", sha256="y", state=state)
    a, b = mk("eval_failed"), mk("eval_passed")
    with pytest.raises(RegistryError, match="passed evaluation"):
        reg.promote(a["id"], approved=True)                           # failed evaluation can never reach production
    with pytest.raises(RegistryError, match="explicit approval"):
        reg.promote(b["id"], approved=False)
    assert reg.status()["production"] is None
    reg.promote(b["id"], approved=True, approver="tester")
    c = mk("eval_passed")
    reg.promote(c["id"], approved=True)
    assert reg.status()["production"] == c["id"] and reg.get(b["id"])["state"] == "superseded"
    assert reg.rollback()["id"] == b["id"] and reg.status()["production"] == b["id"]
    with pytest.raises(RegistryError):
        reg.rollback()
    with pytest.raises(RegistryError):
        reg.update(b["id"], state="trained")                          # a promoted model cannot be quietly demoted


# ---- 6/7. expansion ------------------------------------------------------------------------------------------------------
def _cfg():
    return ModelConfig(50, 2, 16, 2, 8, 32, 24)


def _expand(old_cfg, **changes):
    from dataclasses import replace
    old = build_model(old_cfg, 4)
    with torch.no_grad():                                             # make every parameter non-trivial
        for p in old.parameters():
            p.add_(torch.randn_like(p) * 0.05)
    new_cfg = replace(old_cfg, **changes)
    state, report = expand_state(old.state_dict(), old_cfg, new_cfg, seed=1)
    new = build_model(new_cfg, 0)
    new.load_state_dict(state)
    return old, new, new_cfg, report


@pytest.mark.parametrize("change", [dict(n_layer=5), dict(d_ff=96), dict(n_head=5), dict(max_seq=48),
                                    dict(n_layer=4, d_ff=64, n_head=4)])
def test_function_preserving_expansions_keep_logits_exactly(change):
    old, new, cfg, rep = _expand(_cfg(), **change)
    assert new.num_params() == cfg.param_count() > _cfg().param_count()
    assert rep["expected_function_preserving"] is True
    d = measure_preservation(old, new, seq=24)
    assert d["max_abs_logit_diff"] < 1e-5 and d["top1_agreement"] == 1.0


def test_d_model_expansion_is_reported_as_not_preserving():
    old, new, cfg, rep = _expand(_cfg(), d_model=24)
    assert rep["expected_function_preserving"] is False and "NOT preserved" in rep["preservation_note"]
    assert new.num_params() == cfg.param_count()
    d = measure_preservation(old, new, seq=24)
    assert d["max_abs_logit_diff"] > 1e-4                              # we measure, and report, the real divergence


def test_compatible_weights_are_copied_and_new_ones_are_identity_or_random():
    old, new, cfg, rep = _expand(_cfg(), n_layer=4, d_ff=64)
    assert torch.equal(new.tok.weight, old.tok.weight) and torch.equal(new.blocks[0].qkv.weight, old.blocks[0].qkv.weight)
    assert torch.equal(new.blocks[1].fc.weight[:32], old.blocks[1].fc.weight)              # old FFN rows copied
    assert torch.count_nonzero(new.blocks[1].out.weight[:, 32:]) == 0                      # new units output zero
    assert torch.count_nonzero(new.blocks[3].proj.weight) == 0 and torch.count_nonzero(new.blocks[3].out.weight) == 0
    assert torch.count_nonzero(new.blocks[3].qkv.weight) > 0                              # new block input side is random
    actions = rep["tensor_actions"]
    assert actions["copied"] > 0 and actions["identity-initialised"] > 0 and "optimizer" in rep


def test_incompatible_expansions_are_rejected():
    from dataclasses import replace
    old = build_model(_cfg(), 0)
    for bad in (dict(n_layer=1), dict(d_ff=16), dict(vocab_size=60), dict(head_dim=16), {}):
        with pytest.raises(ExpansionError):
            expand_state(old.state_dict(), _cfg(), replace(_cfg(), **bad))
    with pytest.raises(ExpansionError):
        plan_to_target(_cfg(), 100)                                   # target below the source size


def test_plan_to_target_reaches_requested_size_and_lab_expand_keeps_source(tiny, tmp_path):
    lab, ds = tiny
    own = Lab(tmp_path)
    own_ds = own.make_synthetic_dataset(n_per_label=40, seed=5)
    src = own.init_model("x", own_ds["path"], size="10K", seed=2, max_seq=96)
    src_sha = sha256_file(src["checkpoint"])
    out = own.expand(src["id"], 100_000)
    new = out["model"]
    assert abs(new["param_count"] - 100_000) / 100_000 < 0.3 and new["state"] == "expanded" and new["origin"] == "expanded"
    assert new["param_count"] == ModelConfig.from_dict(new["config"]).param_count()
    assert sha256_file(src["checkpoint"]) == src_sha and own.registry.get(src["id"])["state"] == "random_init"
    assert out["report"]["requires_training"] and out["report"]["measured"]["samples"] > 0
    ev = own.evaluate(new["id"], own_ds["path"], limit=40)
    assert ev["state"] == "eval_failed"                               # untrained: cannot be accepted
    assert not own.registry.status()["best"]


# ---- orchestration, memory refusal, full small cycle --------------------------------------------------------------------------
def test_memory_guard_blocks_unsafe_jobs(tiny, tmp_path, monkeypatch):
    lab = Lab(tmp_path)
    ds = lab.make_synthetic_dataset(n_per_label=40, seed=5)
    monkeypatch.setattr(memory, "system_memory", lambda: {"total": 16 * 1024**3, "available": 300 * 1024**2})
    with pytest.raises(LabError, match="cannot instantiate"):
        lab.init_model("big", ds["path"], size="1M", max_seq=96)
    assert not lab.registry.models()                                  # nothing was created
    monkeypatch.undo()
    entry = lab.init_model("ok", ds["path"], size="10K", max_seq=96)
    monkeypatch.setattr(memory, "system_memory", lambda: {"total": 16 * 1024**3, "available": 300 * 1024**2})
    with pytest.raises(LabError, match="REFUSED|CONFIRMATION_NEEDED"):
        lab.train(entry["id"], ds["path"], TrainConfig(steps=5, batch_size=16))


def test_full_cycle_init_train_evaluate_promote(tmp_path):
    lab = Lab(tmp_path)
    ds = lab.make_synthetic_dataset(n_per_label=120, seed=9)
    e0 = lab.init_model("cycle", ds["path"], size="100K", seed=4, max_seq=96)
    assert e0["state"] == "random_init" and e0["seed"] == 4 and "no pretrained" in e0["notes"]["initialised_from"]
    ev0 = lab.evaluate(e0["id"], ds["path"], limit=60)
    assert ev0["state"] == "eval_failed" and ev0["results"]["val"]["accuracy"] < 0.5          # random model must not pass
    events = []
    res = lab.train(e0["id"], ds["path"], TrainConfig(steps=250, batch_size=32, eval_every=50, ckpt_every=100, lr=3e-3),
                    on_event=events.append)
    assert res["status"] == "completed" and res["model"]["parent"] == e0["id"] and res["model"]["steps"] == 250
    assert any(e["type"] == "val" for e in events) and res["gradient_check"]["max_weight_change"] > 0
    assert Path(e0["checkpoint"]).exists()                            # the parent checkpoint was not overwritten
    ev = lab.evaluate(res["model"]["id"], ds["path"])
    assert ev["results"]["val"]["accuracy"] > ev0["results"]["val"]["accuracy"] + 0.2           # real learning on held-out data
    assert ev["results"]["val"]["loss"] < ev0["results"]["val"]["loss"]
    reg = lab.registry
    if ev["state"] == "eval_passed":
        assert reg.status()["best"] == res["model"]["id"]
        with pytest.raises(RegistryError):
            lab.promote(res["model"]["id"], approved=False)
        assert lab.promote(res["model"]["id"], approved=True)["state"] == "promoted"
    else:
        with pytest.raises(RegistryError):
            lab.promote(res["model"]["id"], approved=True)            # failed evaluation: promotion is impossible


# ---- 14/10: worker protocol --------------------------------------------------------------------------------------------------
class LabProc:
    def __init__(self, tmp):
        env = dict(os.environ, MODEL_LAB_DIR=str(tmp), SPEECH_ENGINE_DATA_DIR=str(tmp / "d"), PYTHONPATH=str(ROOT),
                   PYTHONIOENCODING="utf-8")
        self.p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "model_lab.worker"], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.n = 0
        self.ready = self.read()

    def send(self, action, **kw):
        self.n += 1
        rid = f"r{self.n}"
        self.p.stdin.write((json.dumps({"protocol_version": 2, "request_id": rid, "action": action, **kw}) + "\n").encode())
        self.p.stdin.flush()
        return rid

    def read(self):
        return json.loads(self.p.stdout.readline())

    def until(self, rid):
        events = []
        while True:
            m = self.read()
            if m.get("request_id") == rid and "status" in m:
                return m, events
            if m.get("request_id") == rid:
                events.append(m)

    def close(self):
        self.p.stdin.close()
        try:
            self.p.wait(15)
        except subprocess.TimeoutExpired:
            self.p.kill()


def test_worker_protocol_validation_events_busy_and_cancel(tmp_path):
    w = LabProc(tmp_path)
    try:
        assert w.ready["event"] == "ready" and w.ready["worker"] == "model_lab"
        r, _ = w.until(w.send("health"))
        assert r["status"] == "success" and r["torch_available"] is True and r["memory"]["available"] > 0
        w.p.stdin.write(b"not json\n"); w.p.stdin.flush()
        assert w.read()["code"] == "MALFORMED_REQUEST"
        assert w.until(w.send("explode"))[0]["code"] == "UNKNOWN_ACTION"
        assert w.until(w.send("train", model_id="nope", dataset="x", steps=-5))[0]["code"] == "INVALID_REQUEST"
        assert w.until(w.send("promote", model_id="x"))[0]["code"] == "INVALID_REQUEST"      # no approval flag
        r, _ = w.until(w.send("make_dataset", n_per_label=40))
        ds = r["manifest"]["path"]
        r, _ = w.until(w.send("init_model", name="w", dataset=ds, size="10K"))
        mid = r["model"]["id"]
        assert r["model"]["state"] == "random_init"
        r, _ = w.until(w.send("estimate", model_id=mid, batch_size=16))
        assert r["decision"]["verdict"] in ("ok", "needs_confirmation") and r["total"] > r["weights"]
        rid = w.send("train", model_id=mid, dataset=ds, steps=4000, batch_size=16, eval_every=20)
        busy = w.send("evaluate", model_id=mid, dataset=ds)
        assert w.until(busy)[0]["code"] == "BUSY"
        got_metric = False
        while not got_metric:
            m = w.read()
            got_metric = m.get("event") == "metric" and m["request_id"] == rid
        assert m["metric"]["type"] in ("train", "val") and m["metric"]["loss"] > 0
        w.until(w.send("cancel", target=rid))
        # drain until the train reply
        while True:
            m = w.read()
            if m.get("request_id") == rid and "status" in m:
                break
        assert m["status"] == "cancelled" and m["result"]["steps_done"] < 4000 and Path(m["result"]["run_dir"], "latest.pt").exists()
        r, _ = w.until(w.send("lab_status"))
        assert r["models"][0]["id"] == mid and r["production"] is None
    finally:
        w.close()


def test_worker_without_torch_still_starts_and_explains(tmp_path):
    """Simulates a missing optional dependency: speech must never depend on it, and the lab says so clearly."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "torch.py").write_text("raise ImportError('torch is deliberately unavailable in this test')\n")
    env = dict(os.environ, PYTHONPATH=f"{shim}{os.pathsep}{ROOT}", MODEL_LAB_DIR=str(tmp_path), SPEECH_ENGINE_DATA_DIR=str(tmp_path / "d"))
    p = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "model_lab.worker"], cwd=ROOT, env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        assert json.loads(p.stdout.readline())["event"] == "ready"
        p.stdin.write((json.dumps({"protocol_version": 2, "request_id": "a", "action": "health"}) + "\n").encode()); p.stdin.flush()
        r = json.loads(p.stdout.readline())
        assert r["status"] == "success" and r["torch_available"] is False and "PyTorch is not installed" in r["problem"]
        p.stdin.write((json.dumps({"protocol_version": 2, "request_id": "b", "action": "lab_status"}) + "\n").encode()); p.stdin.flush()
        assert json.loads(p.stdout.readline())["code"] == "TORCH_MISSING"
    finally:
        p.stdin.close()
        p.wait(15)


# ---- 15. the speech engine works independently of the lab ---------------------------------------------------------------------
def test_speech_engine_does_not_import_model_lab_or_torch():
    code = ("import sys; import speech_engine, speech_engine.worker, speech_engine.engine; "
            "print('model_lab' in sys.modules, 'torch' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT)))
    assert out.stdout.split() == ["False", "False"], out.stderr
