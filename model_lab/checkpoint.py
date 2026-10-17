"""Atomic, verifiable checkpoints. A write goes to a temp file and is renamed only when complete, so a crash or cancel can never
corrupt an existing checkpoint, and existing checkpoints are never overwritten by a new model."""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path

import torch

from .config import ModelConfig

SCHEMA = 1


class CheckpointError(Exception):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def save_checkpoint(path: Path, model, cfg: ModelConfig, *, meta: dict, optimizer=None, trainer_state: dict | None = None,
                    overwrite: bool = False) -> dict:
    """Write ``path`` atomically. Refuses to replace an existing file unless ``overwrite`` (used only for 'latest' resume files)."""
    path = Path(path)
    if path.exists() and not overwrite:
        raise CheckpointError(f"{path} already exists; checkpoints are never overwritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": SCHEMA, "config": cfg.to_dict(), "param_count": cfg.param_count(),
               "model": {k: v.detach().cpu() for k, v in model.state_dict().items()},
               "meta": {**meta, "saved_at": time.time()}}
    if optimizer is not None:
        payload["optimizer"] = optimizer.state_dict()
    if trainer_state is not None:
        payload["trainer"] = trainer_state
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".part")
    os.close(fd)
    try:
        torch.save(payload, tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"path": str(path), "sha256": sha256_file(path), "bytes": path.stat().st_size}


def load_checkpoint(path: Path, expected_sha256: str | None = None) -> dict:
    path = Path(path)
    if not path.is_file():
        raise CheckpointError(f"checkpoint not found: {path}")
    if expected_sha256 and sha256_file(path) != expected_sha256:
        raise CheckpointError(f"checksum mismatch for {path} (file changed or corrupted)")
    try:
        ck = torch.load(path, map_location="cpu", weights_only=False)
    except Exception as exc:
        raise CheckpointError(f"cannot read {path}: {exc}") from exc
    validate_checkpoint(ck)
    return ck


def validate_checkpoint(ck: dict) -> ModelConfig:
    if not isinstance(ck, dict) or ck.get("schema") != SCHEMA:
        raise CheckpointError("not a Model Lab checkpoint (schema mismatch)")
    cfg = ModelConfig.from_dict(ck["config"])
    if cfg.param_count() != ck.get("param_count"):
        raise CheckpointError("recorded parameter count does not match the configuration")
    n = sum(v.numel() for v in ck["model"].values())
    if n != cfg.param_count():
        raise CheckpointError(f"tensor sizes ({n}) do not match the configuration ({cfg.param_count()})")
    for k, v in ck["model"].items():
        if not torch.isfinite(v).all():
            raise CheckpointError(f"non-finite values in {k}")
    return cfg


def model_from_checkpoint(ck: dict):
    """Rebuild a model from a checkpoint. The random init is immediately overwritten by the saved weights."""
    from .model import build_model
    cfg = validate_checkpoint(ck)
    m = build_model(cfg, seed=0)
    m.load_state_dict(ck["model"])
    return m, cfg
