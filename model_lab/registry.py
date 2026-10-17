"""Architecture / checkpoint registry with an explicit promotion state machine.

States:  random_init | expanded -> trained -> eval_passed | eval_failed -> promoted (production)
Promotion needs (1) eval_passed and (2) an explicit approval flag from the caller. The previous production model is kept and
``rollback`` restores it. Nothing here is ever promoted automatically.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from speech_engine.paths import data_dir, read_json, write_json_atomic

STATES = ("random_init", "expanded", "trained", "eval_passed", "eval_failed", "promoted", "superseded")


class RegistryError(Exception):
    pass


def lab_dir() -> Path:
    env = os.environ.get("MODEL_LAB_DIR")
    d = Path(env) if env else data_dir() / "model_lab"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else lab_dir()
        self.path = self.root / "registry.json"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- storage ------------------------------------------------------------------------------
    def _load(self) -> dict:
        return read_json(self.path, None) or {"schema": 1, "models": [], "production": None, "production_history": [], "best": None}

    def _save(self, d: dict) -> None:
        write_json_atomic(self.path, d)

    def models(self) -> list[dict]:
        return self._load()["models"]

    def get(self, model_id: str) -> dict:
        for m in self._load()["models"]:
            if m["id"] == model_id:
                return m
        raise RegistryError(f"unknown model {model_id!r}")

    def status(self) -> dict:
        d = self._load()
        return {"production": d["production"], "best": d["best"], "count": len(d["models"]),
                "production_history": d["production_history"]}

    # ---- mutation -----------------------------------------------------------------------------
    def register(self, *, name: str, config: dict, param_count: int, checkpoint: str, sha256: str, state: str,
                 seed: int | None = None, parent: str | None = None, dataset_version: str | None = None,
                 steps: int = 0, metrics: dict | None = None, notes: dict | None = None, origin: str = "scratch") -> dict:
        if state not in STATES:
            raise RegistryError(f"invalid state {state!r}")
        d = self._load()
        existing = [m for m in d["models"] if m["name"] == name]
        entry = {"id": uuid.uuid4().hex[:12], "name": name, "version": len(existing) + 1,
                 "created": time.time(), "state": state, "origin": origin,        # origin: scratch | expanded (never "pretrained")
                 "parent": parent, "config": config, "param_count": param_count, "layers": config["n_layer"],
                 "hidden": config["d_model"], "heads": config["n_head"], "vocab_size": config["vocab_size"],
                 "seq_len": config["max_seq"], "precision": config["precision"], "seed": seed,
                 "dataset_version": dataset_version, "steps": steps, "metrics": metrics or {},
                 "checkpoint": checkpoint, "sha256": sha256, "notes": notes or {}}
        d["models"].append(entry)
        self._save(d)
        return entry

    def update(self, model_id: str, **fields) -> dict:
        d = self._load()
        for m in d["models"]:
            if m["id"] == model_id:
                if "state" in fields and fields["state"] not in STATES:
                    raise RegistryError(f"invalid state {fields['state']!r}")
                if m["state"] == "promoted" and fields.get("state") not in (None, "promoted", "superseded"):
                    raise RegistryError("a promoted model cannot be changed except by promoting another or rolling back")
                m.update(fields)
                self._save(d)
                return m
        raise RegistryError(f"unknown model {model_id!r}")

    def set_best(self, model_id: str) -> None:
        d = self._load()
        if not any(m["id"] == model_id for m in d["models"]):
            raise RegistryError(f"unknown model {model_id!r}")
        d["best"] = model_id
        self._save(d)

    def promote(self, model_id: str, approved: bool, approver: str = "") -> dict:
        """Make ``model_id`` the production model. Requires eval_passed AND explicit approval."""
        if approved is not True:
            raise RegistryError("promotion needs explicit approval (approved=True)")
        d = self._load()
        target = next((m for m in d["models"] if m["id"] == model_id), None)
        if target is None:
            raise RegistryError(f"unknown model {model_id!r}")
        if target["state"] != "eval_passed":
            raise RegistryError(f"only models that passed evaluation can be promoted (state is {target['state']!r})")
        if d["production"]:
            prev = next(m for m in d["models"] if m["id"] == d["production"])
            prev["state"] = "superseded"
            d["production_history"].append(prev["id"])
        target["state"] = "promoted"
        target["promoted_at"] = time.time()
        target["approved_by"] = approver or "user"
        d["production"] = target["id"]
        self._save(d)
        return target

    def rollback(self) -> dict:
        d = self._load()
        if not d["production_history"]:
            raise RegistryError("no previous production model to roll back to")
        cur = next((m for m in d["models"] if m["id"] == d["production"]), None)
        prev_id = d["production_history"].pop()
        prev = next(m for m in d["models"] if m["id"] == prev_id)
        if cur:
            cur["state"] = "eval_passed"           # it did pass evaluation; it is simply no longer in production
        prev["state"] = "promoted"
        d["production"] = prev_id
        self._save(d)
        return prev

    def checkpoint_dir(self, name: str) -> Path:
        p = self.root / "models" / name
        p.mkdir(parents=True, exist_ok=True)
        return p
