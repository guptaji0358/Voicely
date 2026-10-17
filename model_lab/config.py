"""Architecture configuration and analytic parameter counts (no tensors are allocated here).

The count formula is verified against a real PyTorch model in tests/test_model_lab_model.py, so presets such as 1B or 8B can be
described and costed without ever instantiating them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace

SPECIALS = ("<pad>", "<bos>", "<eos>", "<unk>")
PAD, BOS, EOS, UNK = 0, 1, 2, 3
PRECISIONS = ("fp32", "bf16", "fp16")


@dataclass(frozen=True)
class ModelConfig:
    vocab_size: int
    n_layer: int
    d_model: int
    n_head: int
    head_dim: int
    d_ff: int
    max_seq: int
    precision: str = "fp32"          # precision requested for training/inference (weights are stored fp32)
    dropout: float = 0.0

    def __post_init__(self):
        for k in ("vocab_size", "n_layer", "d_model", "n_head", "head_dim", "d_ff", "max_seq"):
            v = getattr(self, k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise ValueError(f"{k} must be a positive integer (got {v!r})")
        if self.vocab_size <= len(SPECIALS):
            raise ValueError("vocab_size must exceed the number of special tokens")
        if self.precision not in PRECISIONS:
            raise ValueError(f"precision must be one of {PRECISIONS}")
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")

    @property
    def attn_dim(self) -> int:
        return self.n_head * self.head_dim

    def param_count(self) -> int:
        """Exact trainable-parameter count (tied input/output embeddings)."""
        d, a, f = self.d_model, self.attn_dim, self.d_ff
        emb = self.vocab_size * d + self.max_seq * d
        per_layer = (2 * d) + (d * 3 * a + 3 * a) + (a * d + d) + (2 * d) + (d * f + f) + (f * d + d)
        return emb + self.n_layer * per_layer + 2 * d

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ModelConfig":
        extra = set(d) - set(ModelConfig.__dataclass_fields__)
        if extra:
            raise ValueError(f"unknown config keys: {sorted(extra)}")
        return ModelConfig(**d)

    def json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


def _shape_for(d_model: int) -> tuple[int, int]:
    head_dim = 4 if d_model < 32 else 16 if d_model < 128 else 64
    return max(1, d_model // head_dim), head_dim


def _layers_for(target: int) -> int:
    for limit, n in ((3_000, 1), (30_000, 2), (300_000, 3), (3_000_000, 4), (30_000_000, 6),
                     (300_000_000, 12), (3_000_000_000, 24)):
        if target < limit:
            return n
    return 32


def config_for_target(target_params: int, vocab_size: int, max_seq: int, precision: str = "fp32") -> ModelConfig:
    """A reasonable shape whose parameter count is as close as possible to ``target_params``.

    This only SIZES an architecture. Whether it can be trained on the available machine is decided by memory.py.
    """
    if target_params < 1:
        raise ValueError("target_params must be positive")
    n_layer = _layers_for(target_params)
    hi = 1
    while hi <= 1 << 16:                                   # bracket d_model; params grow monotonically with it
        n_head, hd = _shape_for(hi)
        if ModelConfig(vocab_size, n_layer, hi, n_head, hd, 4 * hi, max_seq).param_count() >= target_params:
            break
        hi *= 2
    best = None
    for d in range(max(1, hi // 2), hi + 1):
        n_head, hd = _shape_for(d)
        c = ModelConfig(vocab_size, n_layer, d, n_head, hd, 4 * d, max_seq, precision)
        err = abs(c.param_count() - target_params)
        if best is None or err < best[0]:
            best = (err, c)
    return best[1]


# Named sizes. These are ARCHITECTURE TARGETS; "can this machine train it?" is a separate question.
SIZE_TARGETS = {"1K": 1_000, "10K": 10_000, "100K": 100_000, "1M": 1_000_000, "10M": 10_000_000,
                "100M": 100_000_000, "1B": 1_000_000_000, "8B": 8_000_000_000}


def preset(name: str, vocab_size: int, max_seq: int, precision: str = "fp32") -> ModelConfig:
    if name not in SIZE_TARGETS:
        raise ValueError(f"unknown size {name!r}; choose one of {', '.join(SIZE_TARGETS)}")
    return config_for_target(SIZE_TARGETS[name], vocab_size, max_seq, precision)


def with_changes(cfg: ModelConfig, **kw) -> ModelConfig:
    return replace(cfg, **kw)
