"""Learning from the person's own generations + the single-model workflow. Real PyTorch; the ASR labels are synthetic inputs to the
store (the recogniser itself is covered by test_verification / test_real_optional)."""

import random
from pathlib import Path

import pytest

from model_lab import data
from model_lab.checkpoint import sha256_file
from model_lab.lab import Lab, LabError
from model_lab.train import TrainConfig
from speech_engine.experience import ExperienceStore


# ---- the experience store: consent, privacy rules, labels ---------------------------------------------------------------
def chunks(*texts):
    return [{"index": i, "text": t} for i, t in enumerate(texts)]


def test_nothing_is_stored_without_consent_and_stopping_is_immediate(tmp_path):
    st = ExperienceStore(tmp_path)
    assert st.consent()["learn_from_generations"] is False
    assert st.add_generation("s1", chunks("Hello world."), "en-us", "original", "local") == 0
    assert st.stats()["total"] == 0 and not (tmp_path / "records.json").exists()
    st.set_consent(True)
    assert st.add_generation("s1", chunks("Hello world.", "Second one."), "en-us", "original", "local") == 2
    st.set_consent(False)
    assert st.add_generation("s2", chunks("Not stored."), "en-us", "original", "local") == 0
    assert not st.label_from_recheck("s1", 0, "verified", False, [])        # off: no new labels either
    assert st.stats()["total"] == 2


def test_labels_come_only_from_real_rechecks_and_are_set_once(tmp_path):
    st = ExperienceStore(tmp_path)
    st.set_consent(True)
    st.add_generation("s", chunks("A", "B", "C", "D", "E"), "en-us", "v", "local")
    assert st.label_from_recheck("s", 0, "verified", False, [])
    assert st.label_from_recheck("s", 1, "needs_review", False, ["substitution", "substitution"])
    assert st.label_from_recheck("s", 2, "failed", False, [])
    assert not st.label_from_recheck("s", 3, "verified", True, [])                       # MOCK recogniser: never a label
    assert not st.label_from_recheck("s", 4, "failed", False, [], recogniser_error=True)  # the recogniser broke, not the speech
    assert not st.label_from_recheck("s", 4, "not_checked", False, [])
    assert not st.label_from_recheck("s", 0, "needs_review", False, [])                  # first check of the original wins
    assert not st.label_from_recheck("other", 0, "verified", False, [])
    s = st.stats()
    assert (s["yes"], s["no"], s["unlabelled"]) == (2, 1, 2)
    rec = {r["text"]: r for r in st.list()}
    assert rec["B"]["issue_types"] == ["substitution"] and rec["A"]["label_source"] == "asr-recheck"


def test_inspect_export_and_delete(tmp_path):
    st = ExperienceStore(tmp_path / "e")
    st.set_consent(True)
    st.add_generation("s", chunks("One.", "Two."), "en-us", "v", "local")
    assert st.add_generation("s", chunks("One."), "en-us", "v", "local") == 0            # same session+chunk: no duplicate
    out = st.export(tmp_path / "out")
    assert Path(out["path"]).is_file() and out["records"] == 2
    with pytest.raises(FileExistsError):
        st.export(tmp_path / "out")
    st.delete(st.list()[0]["id"])
    assert st.stats()["total"] == 1
    assert st.delete_all() == 1 and st.stats()["total"] == 0


def test_generation_hook_is_a_noop_without_consent_and_records_with_it(tmp_path, monkeypatch):
    from speech_engine.backends.mock_backend import MockBackend
    from speech_engine.worker_qa import QAMixin
    import threading
    monkeypatch.setenv("SPEECH_ENGINE_ALLOW_MOCK", "1")

    class W(QAMixin):
        from speech_engine.qa import QA
        qa = None

        def progress(self, *a): pass

    from speech_engine.qa import QA
    from speech_engine.voice_profiles import ORIGINAL
    w = W()
    w.qa = QA()
    backend = MockBackend()
    req = {"session_dir": str(tmp_path / "s1")}
    w.synthesize_session("r", req, threading.Event(), backend, ORIGINAL, "First sentence here. Second sentence follows.",
                         tmp_path / "o1.wav", "wav")
    assert ExperienceStore().stats()["total"] == 0                                        # consent is off by default
    ExperienceStore().set_consent(True)
    w.synthesize_session("r", {"session_dir": str(tmp_path / "s2")}, threading.Event(), backend, ORIGINAL,
                         "First sentence here. Second sentence follows.", tmp_path / "o2.wav", "wav")
    st = ExperienceStore().stats()
    assert st["total"] >= 1 and st["labelled"] == 0                                       # captured, still waiting for a recheck


def test_recheck_with_mock_asr_never_creates_labels(tmp_path, monkeypatch):
    import json, threading
    from speech_engine.backends.mock_backend import MockBackend
    from speech_engine.qa import QA
    from speech_engine.session import Session
    from speech_engine.verification import Thresholds
    from speech_engine.voice_profiles import ORIGINAL
    from speech_engine.backends.base import SynthParams
    monkeypatch.setenv("SPEECH_ENGINE_ALLOW_MOCK", "1")
    ExperienceStore().set_consent(True)
    qa = QA()
    be = MockBackend()
    s = Session.create(str(tmp_path / "s"), "Hello there. Another sentence.", be, ORIGINAL, SynthParams(), "en-us", qa.dictionary,
                       threading.Event(), lambda *a: None, qa.versions("mock"))
    ExperienceStore().add_generation(s.m["session_id"], s.chunks, "en-us", "original", "mock")
    asr_file = tmp_path / "asr.json"
    asr_file.write_text(json.dumps({f"{c['index']:04d}.g0.wav": c["text"] for c in s.chunks}), encoding="utf-8")
    monkeypatch.setenv("SPEECH_ENGINE_MOCK_ASR_FILE", str(asr_file))
    qa.recheck(s, "accepted", None, qa.make_asr("mock"), Thresholds(min_rate=0, max_rate=1e9), threading.Event(), lambda *a: None)
    assert ExperienceStore().stats()["labelled"] == 0                                     # mocked recogniser: no labels


# ---- dataset from experience --------------------------------------------------------------------------------------------
def fake_store(root, n_yes=40, n_no=40, seed=1):
    st = ExperienceStore(root)
    st.set_consent(True)
    rng = random.Random(seed)
    risky = ["Dr. Patel paid $45 on March 3rd for 5 km", "NASA read the record", "Call 555-0100 about the 12 kg order"]
    plain = ["The quiet garden grew slowly", "We walked beside the calm river", "A gentle breeze moved the paper"]
    items = [(rng.choice(risky) + f" number {i}", "needs_review") for i in range(n_yes)] + \
            [(rng.choice(plain) + f" again {i}", "verified") for i in range(n_no)]
    rng.shuffle(items)
    for i, (t, status) in enumerate(items):
        st.add_generation(f"sess{i}", chunks(t), "en-us", "original", "local")
        st.label_from_recheck(f"sess{i}", 0, status, False, ["substitution"] if status != "verified" else [])
    return st


def test_experience_dataset_needs_enough_data_and_both_outcomes(tmp_path):
    lab = Lab(tmp_path / "lab")
    st = ExperienceStore(tmp_path / "e")
    st.set_consent(True)
    st.add_generation("s", chunks("only unlabelled"), "en-us", "v", "local")
    with pytest.raises(LabError, match="NOT_ENOUGH_DATA.*Recheck Speech"):
        lab.build_experience_dataset(st)
    fake = fake_store(tmp_path / "e2", n_yes=50, n_no=3)
    with pytest.raises(LabError, match="NOT_ENOUGH_DATA"):
        lab.build_experience_dataset(fake)                                                # one outcome nearly missing


def test_experience_dataset_is_traceable_leakage_free_and_time_held_out(tmp_path):
    lab = Lab(tmp_path / "lab")
    st = fake_store(tmp_path / "e")
    m = lab.build_experience_dataset(st)
    assert m["task"] == "needs_review" and m["labels"] == ["no", "yes"] and "USER GENERATIONS" in m["provenance"]
    manifest, splits = data.load_dataset(m["path"])
    assert manifest["counts"]["regression"] >= 5
    newest_in_reg = min(r["created"] for r in splits["regression"])
    assert all(r["created"] <= newest_in_reg for s in ("train", "val", "test") for r in splits[s])    # regression = newest documents
    assert all(r["source"] == "experience" and r["label_source"] == "asr-recheck" for s in splits.values() for r in s)
    for v in range(3):                                                                    # old dataset versions are pruned
        lab.build_experience_dataset(st)
    assert len([d for d in lab.datasets() if d["name"] == "my-generations"]) == 2


def test_task_tag_is_part_of_the_prompt(tmp_path):
    from model_lab.tokenizer import Tokenizer
    from model_lab.lab import BASE_ALPHABET
    tok = Tokenizer.from_alphabet(BASE_ALPHABET)
    a, pl_a = data.encode_example(tok, {"text": "Hi", "target": "yes", "task": "needs_review"})
    b, pl_b = data.encode_example(tok, {"text": "Hi", "target": "plain", "task": "review_category"})
    assert tok.decode(a[1:pl_a]).startswith("R|Hi") and tok.decode(b[1:pl_b]).startswith("C|Hi")
    assert tok.size < 130 and all(tok.index.get(c) for c in "Hello, world! 123 $%&")      # ordinary text is covered by the fixed vocabulary


# ---- the single-model workflow ------------------------------------------------------------------------------------------
def tiny_cfg(**kw):
    return TrainConfig(steps=60, batch_size=16, eval_every=20, ckpt_every=30, log_every=10, warmup_steps=5, lr=3e-3, **kw)


def test_only_one_model_exists_and_replacing_needs_consent(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=40, seed=2)
    a = lab.create_main(ds["path"], size="10K", seed=1)
    assert lab.active()["id"] == a["id"] and len(lab.registry.models()) == 1
    with pytest.raises(LabError, match="MODEL_EXISTS"):
        lab.create_main(ds["path"], size="10K")                                           # never silently adds a second model
    old_sha = a["sha256"]
    b = lab.create_main(ds["path"], size="100K", seed=2, replace=True)
    assert len(lab.registry.models()) == 1 and lab.active()["id"] == b["id"] and b["id"] != a["id"]
    assert sha256_file(b["checkpoint"]) != old_sha and b["param_count"] > a["param_count"]      # the old model's file is gone
    ckpts = list((tmp_path / "lab" / "models").rglob("*.pt"))
    assert len(ckpts) == 1


def test_candidate_must_be_accepted_to_replace_and_old_files_are_deleted(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=80, seed=2)
    act = lab.create_main(ds["path"], size="100K", seed=3)
    res = lab.train_candidate(Path(ds["path"]), TrainConfig(steps=200, batch_size=32, eval_every=50, ckpt_every=100, lr=3e-3))
    cand = res["candidate"]
    assert res["status"] == "completed" and cand["notes"]["role"] == "candidate" and lab.active()["id"] == act["id"]
    assert len(lab.registry.models()) == 2                                               # active + one pending candidate, nothing else
    assert set(res["results"]) == {"candidate", "active"} and cand["metrics"]["evaluation"]["compared_with"] == act["id"]
    assert res["results"]["candidate"]["val"]["accuracy"] > res["results"]["active"]["val"]["accuracy"]
    if cand["state"] == "eval_passed":
        old = Path(act["checkpoint"])
        new = lab.accept_candidate()
        assert lab.active()["id"] == new["id"] and len(lab.registry.models()) == 1 and not old.exists()
        assert len(list((tmp_path / "lab" / "models").rglob("*.pt"))) == 1
    else:
        with pytest.raises(LabError, match="CANDIDATE_NOT_ACCEPTED"):
            lab.accept_candidate()


def test_rejecting_or_failing_candidate_keeps_the_current_model(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=60, seed=2)
    act = lab.create_main(ds["path"], size="10K", seed=3)
    res = lab.train_candidate(Path(ds["path"]), tiny_cfg())
    assert res["candidate"] is not None
    cand_ckpt = Path(res["candidate"]["checkpoint"])
    out = lab.reject_candidate()
    assert out["active"] == act["id"] and not cand_ckpt.exists() and len(lab.registry.models()) == 1
    with pytest.raises(LabError, match="NO_CANDIDATE"):
        lab.accept_candidate()
    import threading
    cancel = threading.Event()
    cancel.set()
    cres = lab.train_candidate(Path(ds["path"]), tiny_cfg(), cancel=cancel)
    assert cres["status"] == "cancelled" and cres["candidate"] is None and len(lab.registry.models()) == 1


def test_a_new_candidate_supersedes_an_undecided_one(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=60, seed=2)
    lab.create_main(ds["path"], size="10K", seed=3)
    lab.train_candidate(Path(ds["path"]), tiny_cfg())
    lab.train_candidate(Path(ds["path"]), tiny_cfg(seed=99))
    assert len(lab.registry.models()) == 2 and len(lab._by_role("candidate")) == 1


def test_expand_active_replaces_the_model_and_requires_training(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=40, seed=2)
    act = lab.create_main(ds["path"], size="10K", seed=3)
    old = Path(act["checkpoint"])
    out = lab.expand_active(60_000, seed=1)
    new = out["model"]
    assert len(lab.registry.models()) == 1 and lab.active()["id"] == new["id"] and not old.exists()
    assert new["param_count"] > 10_000 and new["state"] == "expanded" and out["report"]["requires_training"]
    assert len(list((tmp_path / "lab" / "models").rglob("*.pt"))) == 1


def test_cleanup_models_removes_extras_but_keeps_the_chosen_one(tmp_path):
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=40, seed=2)
    a = lab.init_model("x1", ds["path"], size="10K", max_seq=96)
    b = lab.init_model("x2", ds["path"], size="10K", max_seq=96)
    out = lab.cleanup_models(keep_id=b["id"])
    assert [r["id"] for r in out["removed"]] == [a["id"]] and [m["id"] for m in lab.registry.models()] == [b["id"]]
    assert not (tmp_path / "lab" / "models" / "x1").exists() and Path(b["checkpoint"]).exists()


# ---- end to end: generations -> rechecks -> candidate that actually learned ------------------------------------------------
def test_learning_from_generations_end_to_end(tmp_path):
    lab = Lab(tmp_path / "lab")
    st = fake_store(tmp_path / "e", n_yes=60, n_no=60)
    seed_ds = lab.make_synthetic_dataset(n_per_label=20, seed=2)
    act = lab.create_main(seed_ds["path"], size="100K", seed=5)
    events = []
    res = lab.learn_from_generations(TrainConfig(steps=250, batch_size=32, eval_every=50, ckpt_every=125, lr=3e-3), store=st,
                                     min_labelled=60, min_per_class=15, on_event=events.append)
    assert res["status"] == "completed" and res["experience_dataset"]["counts"]["train"] > 50
    assert any(e["type"] == "evaluating" for e in events) and res["gradient_check"]["max_weight_change"] > 0
    cand, rr = res["candidate"], res["results"]
    assert rr["candidate"]["val"]["accuracy"] > rr["active"]["val"]["accuracy"] and rr["candidate"]["val"]["accuracy"] > 0.7
    assert len(lab.registry.models()) == 2 and lab.active()["id"] == act["id"]            # nothing replaced without approval
    if cand["state"] == "eval_passed":
        lab.accept_candidate()
        assert len(lab.registry.models()) == 1


def test_a_refused_replacement_keeps_the_current_model(tmp_path, monkeypatch):
    from model_lab import memory
    lab = Lab(tmp_path / "lab")
    ds = lab.make_synthetic_dataset(n_per_label=40, seed=2)
    a = lab.create_main(ds["path"], size="10K", seed=1)
    monkeypatch.setattr(memory, "system_memory", lambda: {"total": 16 * 1024**3, "available": 300 * 1024**2})
    with pytest.raises(LabError, match="cannot instantiate"):
        lab.create_main(ds["path"], size="1B", replace=True)                 # too big for free memory
    assert lab.active()["id"] == a["id"] and Path(a["checkpoint"]).exists() and len(lab.registry.models()) == 1
