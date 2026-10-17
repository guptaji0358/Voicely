"""Model Lab: architecture, random initialisation, tokenizer, checkpoints, memory budget. Real PyTorch, no mocks."""

import math

import pytest
import torch

from model_lab import memory
from model_lab.checkpoint import CheckpointError, load_checkpoint, model_from_checkpoint, save_checkpoint, sha256_file
from model_lab.config import EOS, ModelConfig, SIZE_TARGETS, config_for_target, preset
from model_lab.model import build_model
from model_lab.tokenizer import Tokenizer


def small(**kw):
    base = dict(vocab_size=40, n_layer=2, d_model=16, n_head=2, head_dim=8, d_ff=32, max_seq=24)
    base.update(kw)
    return ModelConfig(**base)


# ---- 3. parameter counts match the configuration ------------------------------------------------------------------
@pytest.mark.parametrize("cfg", [small(), small(n_layer=1, d_model=8, n_head=1, head_dim=4, d_ff=8),
                                 small(n_head=3, head_dim=5, d_model=12, d_ff=50), small(vocab_size=300, max_seq=64, n_layer=3)])
def test_param_count_formula_matches_real_model(cfg):
    assert build_model(cfg, 0).num_params() == cfg.param_count()


@pytest.mark.parametrize("name", ["1K", "10K", "100K", "1M", "10M"])
def test_presets_hit_their_targets(name):
    cfg = preset(name, vocab_size=80, max_seq=96)
    target = SIZE_TARGETS[name]
    assert abs(cfg.param_count() - target) / target < 0.15          # 1K is limited by the embedding tables
    if target <= 1_000_000:
        assert build_model(cfg, 0).num_params() == cfg.param_count()


def test_huge_presets_are_described_not_allocated():
    for name in ("100M", "1B", "8B"):
        cfg = preset(name, 80, 96)
        assert abs(cfg.param_count() - SIZE_TARGETS[name]) / SIZE_TARGETS[name] < 0.02     # no tensors were created


def test_config_validation():
    with pytest.raises(ValueError):
        small(n_layer=0)
    with pytest.raises(ValueError):
        small(precision="fp8")
    with pytest.raises(ValueError):
        ModelConfig.from_dict({**small().to_dict(), "bogus": 1})


# ---- 1/2. random initialisation, nothing pretrained ------------------------------------------------------------------
def test_weights_are_random_and_seed_controlled():
    a, b, c = build_model(small(), 1), build_model(small(), 1), build_model(small(), 2)
    wa, wb, wc = a.tok.weight, b.tok.weight, c.tok.weight
    assert torch.equal(wa, wb) and not torch.equal(wa, wc)           # reproducible per seed, different across seeds
    assert 0.01 < float(wa.detach().std()) < 0.03                              # drawn from N(0, 0.02), not zeros or constants
    assert a.init_seed == 1 and c.init_seed == 2
    assert not torch.equal(a.blocks[0].qkv.weight, a.blocks[1].qkv.weight)      # layers are not copies of each other


def test_scratch_construction_never_loads_a_checkpoint(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("scratch initialisation tried to load weights")
    monkeypatch.setattr(torch, "load", boom)
    monkeypatch.setattr(torch.nn.Module, "load_state_dict", boom)
    m = build_model(small(), None)                                    # seed None: a fresh seed is drawn and recorded
    assert isinstance(m.init_seed, int) and m.num_params() > 0


# ---- forward, loss, causality, generation ----------------------------------------------------------------------------
def test_forward_shapes_and_loss_value():
    cfg = small()
    m = build_model(cfg, 0)
    x = torch.randint(4, cfg.vocab_size, (3, 10))
    y = torch.randint(4, cfg.vocab_size, (3, 10))
    logits, loss = m(x, y)
    assert logits.shape == (3, 10, cfg.vocab_size)
    manual = torch.nn.functional.cross_entropy(logits.reshape(-1, cfg.vocab_size), y.reshape(-1))
    assert torch.allclose(loss, manual)
    assert abs(float(loss) - math.log(cfg.vocab_size)) < 0.5           # an untrained model is near uniform
    with pytest.raises(ValueError):
        m(torch.zeros(1, cfg.max_seq + 1, dtype=torch.long))


def test_loss_ignores_masked_targets():
    m = build_model(small(), 0)
    x = torch.randint(4, 40, (2, 8))
    y = torch.randint(4, 40, (2, 8))
    y2 = y.clone()
    y2[:, :4] = -100
    l_all, l_masked = m(x, y)[1], m(x, y2)[1]
    logits = m(x)[0]
    want = torch.nn.functional.cross_entropy(logits[:, 4:].reshape(-1, 40), y[:, 4:].reshape(-1))
    assert torch.allclose(l_masked, want) and not torch.allclose(l_all, l_masked)


def test_attention_is_causal():
    m = build_model(small(), 0).eval()
    x = torch.randint(4, 40, (1, 12))
    x2 = x.clone()
    x2[0, 8:] = (x2[0, 8:] + 1) % 36 + 4                              # change only the future
    a, b = m(x)[0], m(x2)[0]
    assert torch.allclose(a[:, :8], b[:, :8], atol=1e-6) and not torch.allclose(a[:, 9:], b[:, 9:])


def test_generation_greedy_deterministic_and_stops_at_eos():
    m = build_model(small(), 3)
    p = torch.tensor([[1, 5, 6]])
    g1, g2 = m.generate(p, 8, eos_id=None), m.generate(p, 8, eos_id=None)
    assert g1.shape == (1, 11) and torch.equal(g1, g2)
    with torch.no_grad():
        m.tok.weight[EOS] *= 0
        m.tok.weight[EOS] += m.tok.weight[7] * 50                    # force EOS to be the argmax
    out = m.generate(p, 8, eos_id=EOS)
    assert out.shape[1] < 11 and int(out[0, -1]) == EOS
    s = m.generate(p, 5, temperature=1.0, top_k=5, eos_id=None, generator=torch.Generator().manual_seed(0))
    assert s.shape == (1, 8)


# ---- tokenizer ------------------------------------------------------------------------------------------------------
def test_tokenizer_char_and_bpe_roundtrip(tmp_path):
    texts = ["the cat sat on the mat", "the dog sat on the log"] * 5
    ch = Tokenizer.train(texts)
    assert ch.decode(ch.encode("the cat")) == "the cat" and ch.encode("Z")[0] == 3          # unknown -> <unk>
    bpe = Tokenizer.train(texts, vocab_size=ch.size + 12)
    assert bpe.merges and bpe.size > ch.size
    assert bpe.decode(bpe.encode("the dog sat")) == "the dog sat"
    assert len(bpe.encode("the cat sat on the mat")) < len(ch.encode("the cat sat on the mat"))
    bpe.save(tmp_path / "t.json")
    assert Tokenizer.load(tmp_path / "t.json").encode("the log") == bpe.encode("the log")
    with pytest.raises(ValueError):
        Tokenizer.train(texts, vocab_size=5)


# ---- 5. checkpoints ---------------------------------------------------------------------------------------------------
def test_checkpoint_roundtrip_is_exact_and_never_overwrites(tmp_path):
    cfg = small()
    m = build_model(cfg, 5)
    info = save_checkpoint(tmp_path / "a.pt", m, cfg, meta={"seed": 5})
    assert info["sha256"] == sha256_file(tmp_path / "a.pt")
    m2, cfg2 = model_from_checkpoint(load_checkpoint(tmp_path / "a.pt", info["sha256"]))
    x = torch.randint(4, 40, (2, 9))
    assert cfg2 == cfg and torch.equal(m(x)[0], m2(x)[0])
    with pytest.raises(CheckpointError):
        save_checkpoint(tmp_path / "a.pt", m, cfg, meta={})
    assert not list(tmp_path.glob("*.part"))


def test_corrupted_or_tampered_checkpoint_is_rejected(tmp_path):
    cfg = small()
    m = build_model(cfg, 5)
    info = save_checkpoint(tmp_path / "a.pt", m, cfg, meta={})
    with pytest.raises(CheckpointError):
        load_checkpoint(tmp_path / "a.pt", "0" * 64)
    (tmp_path / "bad.pt").write_bytes(b"not a checkpoint")
    with pytest.raises(CheckpointError):
        load_checkpoint(tmp_path / "bad.pt")
    ck = load_checkpoint(tmp_path / "a.pt")
    ck["model"]["tok.weight"][0, 0] = float("nan")
    with pytest.raises(CheckpointError, match="non-finite"):
        model_from_checkpoint(ck)


def test_failed_save_leaves_existing_checkpoint_intact(tmp_path, monkeypatch):
    cfg = small()
    m = build_model(cfg, 5)
    save_checkpoint(tmp_path / "latest.pt", m, cfg, meta={"v": 1})
    before = sha256_file(tmp_path / "latest.pt")
    real = torch.save

    def failing(obj, path, *a, **k):
        open(path, "wb").write(b"half written")
        raise OSError("disk full")
    monkeypatch.setattr(torch, "save", failing)
    with pytest.raises(OSError):
        save_checkpoint(tmp_path / "latest.pt", m, cfg, meta={"v": 2}, overwrite=True)
    monkeypatch.setattr(torch, "save", real)
    assert sha256_file(tmp_path / "latest.pt") == before and not list(tmp_path.glob("*.part"))


# ---- 12. memory budget ------------------------------------------------------------------------------------------------
GB = 1024**3


def test_budget_decisions():
    ok = memory.check_budget(int(0.5 * GB), available=4 * GB)
    assert ok.verdict == "ok"
    need = memory.check_budget(int(3.5 * GB), available=4 * GB)
    assert need.verdict == "needs_confirmation" and "safety margin" in need.reason
    assert memory.check_budget(int(3.5 * GB), available=4 * GB, confirmed=True).verdict == "ok"
    ref = memory.check_budget(int(5 * GB), available=4 * GB, confirmed=True)
    assert ref.verdict == "refuse"                                    # confirmation cannot override the hard floor


def test_estimates_scale_and_big_models_are_refused_on_this_class_of_machine():
    small_est = memory.estimate_training_bytes(preset("100K", 80, 96), 16)
    mid = memory.estimate_training_bytes(preset("10M", 80, 96), 16)
    big = memory.estimate_training_bytes(preset("1B", 80, 96), 8)
    assert small_est["total"] < mid["total"] < big["total"]
    assert big["weights"] == 4 * preset("1B", 80, 96).param_count()
    for size in ("1B", "8B"):
        est = memory.estimate_training_bytes(preset(size, 80, 96), 1)
        assert memory.check_budget(est["total"], available=4 * GB, confirmed=True).verdict == "refuse"


def test_system_memory_and_peak_are_real_numbers(monkeypatch):
    monkeypatch.delenv("MODEL_LAB_TEST_AVAILABLE_GB")                  # this one measures the real machine
    m = memory.system_memory()
    assert m["total"] > 0 and 0 < m["available"] <= m["total"]
    assert memory.peak_rss() >= memory.current_rss() > 10_000_000
