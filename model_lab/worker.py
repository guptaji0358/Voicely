"""JSON-lines worker for the Model Lab (runs in its OWN process, separate from the speech worker).

    python -m model_lab.worker

Same envelope as speech_engine.worker (protocol v2, request_id, stdout carries only protocol messages). Extra event types:
  {"event":"metric", "request_id":..., "metric": {...}}   training/validation points for the loss chart (real values only)
Torch is imported lazily: without it the worker still starts and `health` says so, so a missing optional dependency never
affects the speech application.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import traceback
from pathlib import Path

from speech_engine.worker import PROTOCOL_VERSION, SUPPORTED_VERSIONS, _Out, log

JOB_ACTIONS = {"train", "evaluate", "make_dataset"}
SYNC_ACTIONS = {"lab_status", "estimate", "init_model", "expand", "promote", "rollback", "history", "delete_dataset",
                "datasets", "dataset_info"}


class BadRequest(Exception):
    pass


def _int(req, key, default=None, lo=1, hi=10**9):
    v = req.get(key, default)
    if v is None and default is None:
        raise BadRequest(f"{key} is required")
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise BadRequest(f"{key} must be an integer between {lo} and {hi}")
    return v


def _str(req, key, required=True, maxlen=500):
    v = req.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v or len(v) > maxlen:
        raise BadRequest(f"{key} must be a non-empty string")
    return v


def _num(req, key, default, lo, hi):
    v = req.get(key, default)
    if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
        raise BadRequest(f"{key} must be a number between {lo} and {hi}")
    return float(v)


class LabWorker:
    def __init__(self, out: _Out):
        self.out = out
        self.job: tuple[str, threading.Event] | None = None
        self.lock = threading.Lock()
        self._lab = None

    # ---- plumbing ---------------------------------------------------------------------------------
    def error(self, rid, code, message, **extra):
        self.out.send({"request_id": rid, "status": "error", "code": code, "message": message, **extra})

    def ok(self, rid, **fields):
        self.out.send({"request_id": rid, "status": "success", **fields})

    def event(self, rid, name, **fields):
        self.out.send({"request_id": rid, "event": name, **fields})

    def lab(self):
        if self._lab is None:
            try:
                from model_lab.lab import Lab
            except ImportError as exc:
                raise RuntimeError(f"TORCH_MISSING: {exc}") from exc
            self._lab = Lab(Path(os.environ["MODEL_LAB_DIR"]) if os.environ.get("MODEL_LAB_DIR") else None)
        return self._lab

    # ---- dispatch ---------------------------------------------------------------------------------
    def handle(self, line: str) -> None:
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("request must be a JSON object")
        except ValueError as exc:
            return self.error(None, "MALFORMED_REQUEST", f"invalid JSON: {exc}")
        rid = req.get("request_id")
        if not isinstance(rid, str) or not rid:
            return self.error(None, "MALFORMED_REQUEST", "missing request_id")
        if req.get("protocol_version") not in SUPPORTED_VERSIONS or isinstance(req.get("protocol_version"), bool):
            return self.error(rid, "UNSUPPORTED_PROTOCOL", f"worker speaks protocol {SUPPORTED_VERSIONS}")
        action = req.get("action")
        try:
            if action == "health":
                self.health(rid)
            elif action == "cancel":
                self.cancel(rid, req)
            elif action == "shutdown":
                self.ok(rid)
                os._exit(0)
            elif action in JOB_ACTIONS:
                self.start_job(rid, action, req)
            elif action in SYNC_ACTIONS:
                self.run_sync(rid, action, req)
            else:
                self.error(rid, "UNKNOWN_ACTION", f"unknown action {action!r}")
        except BadRequest as exc:
            self.error(rid, "INVALID_REQUEST", str(exc))
        except Exception as exc:
            log(traceback.format_exc())
            self.error(rid, *self.classify(exc))

    @staticmethod
    def classify(exc) -> tuple[str, str]:
        msg = f"{type(exc).__name__}: {exc}"
        text = str(exc)
        for prefix, code in (("TORCH_MISSING", "TORCH_MISSING"), ("CONFIRMATION_NEEDED", "CONFIRMATION_NEEDED"),
                             ("REFUSED", "MEMORY_REFUSED")):
            if text.startswith(prefix) or ("LabError" in type(exc).__name__ and text.startswith(prefix)):
                return code, text
        name = type(exc).__name__
        if name in ("RegistryError", "DatasetError", "CheckpointError", "ExpansionError", "TrainingError", "LabError"):
            return name.replace("Error", "").upper() + "_ERROR", text
        return "INTERNAL_ERROR", msg

    # ---- actions ----------------------------------------------------------------------------------
    def health(self, rid):
        info = {"python": sys.version.split()[0], "executable": sys.executable, "torch_available": False, "protocol_version": PROTOCOL_VERSION}
        try:
            from model_lab import memory
            info.update(memory.hardware_report())
            info["torch_available"] = True
            info["memory"] = memory.system_memory()
            from model_lab.registry import lab_dir
            info["lab_dir"] = str(lab_dir())
        except ImportError as exc:
            info["problem"] = f"PyTorch is not installed in this Python ({exc}). Install it into the interpreter chosen for Model Lab."
        self.ok(rid, **info)

    def cancel(self, rid, req):
        target = req.get("target")
        with self.lock:
            if self.job and self.job[0] == target:
                self.job[1].set()
                return self.ok(rid, cancelling=True)
        self.error(rid, "NOT_RUNNING", f"no running job {target!r}")

    def run_sync(self, rid, action, req):
        lab = self.lab()
        if action == "lab_status":
            from model_lab import memory
            self.ok(rid, models=lab.registry.models(), **lab.registry.status(), datasets=lab.datasets(), memory=memory.system_memory(),
                    root=str(lab.root))
        elif action == "datasets":
            self.ok(rid, datasets=lab.datasets())
        elif action == "dataset_info":
            from model_lab.data import load_dataset
            m, _ = load_dataset(Path(_str(req, "dataset")))
            self.ok(rid, manifest=m)
        elif action == "estimate":
            self.ok(rid, **lab.estimate(_str(req, "model_id"), _int(req, "batch_size", 32)))
        elif action == "init_model":
            e = lab.init_model(_str(req, "name", maxlen=60), Path(_str(req, "dataset")), size=_str(req, "size"),
                               max_seq=_int(req, "max_seq", 96, 16, 4096), seed=_int(req, "seed", 0, 0, 2**31 - 1),
                               confirmed=bool(req.get("confirmed")))
            self.ok(rid, model=e)
        elif action == "expand":
            self.ok(rid, **lab.expand(_str(req, "model_id"), _int(req, "target_parameters", None, 1, 10**11),
                                      seed=_int(req, "seed", 0, 0, 2**31 - 1), confirmed=bool(req.get("confirmed"))))
        elif action == "promote":
            if req.get("approved") is not True:
                raise BadRequest("promotion needs approved=true (an explicit user approval)")
            self.ok(rid, model=lab.promote(_str(req, "model_id"), True, _str(req, "approver", False, 80) or ""))
        elif action == "rollback":
            self.ok(rid, model=lab.rollback())
        elif action == "delete_dataset":
            from model_lab.data import delete_dataset
            delete_dataset(Path(_str(req, "dataset")))
            self.ok(rid)
        elif action == "history":
            run_dir = Path(_str(req, "run_dir"))
            pts = []
            f = run_dir / "metrics.jsonl"
            if f.is_file():
                for l in f.read_text(encoding="utf-8").splitlines()[-5000:]:
                    try:
                        pts.append(json.loads(l))
                    except ValueError:
                        pass
            self.ok(rid, points=pts)

    def start_job(self, rid, action, req):
        self.lab()                                  # fail fast (TORCH_MISSING) before claiming the job slot
        with self.lock:
            if self.job:
                return self.error(rid, "BUSY", "another job is running; wait for it or cancel it")
            cancel = threading.Event()
            self.job = (rid, cancel)
        threading.Thread(target=self._run_job, args=(rid, action, req, cancel), daemon=True).start()

    def _run_job(self, rid, action, req, cancel):
        try:
            lab = self.lab()
            if action == "make_dataset":
                m = lab.make_synthetic_dataset(_str(req, "name", False, 60) or "review-category", _int(req, "n_per_label", 400, 10, 100000),
                                               _int(req, "seed", 7, 0, 2**31 - 1))
                return self.ok(rid, manifest=m)
            if action == "evaluate":
                self.event(rid, "progress", progress=5, message="evaluating on validation, test and held-out regression sets")
                r = lab.evaluate(_str(req, "model_id"), Path(_str(req, "dataset")))
                return self.ok(rid, **r)
            from model_lab.train import TrainConfig
            tc = TrainConfig(steps=_int(req, "steps", 1000, 1, 10**7), batch_size=_int(req, "batch_size", 32, 1, 4096),
                             grad_accum=_int(req, "grad_accum", 1, 1, 1024), lr=_num(req, "lr", 3e-3, 1e-7, 1.0),
                             seed=_int(req, "seed", 1234, 0, 2**31 - 1), eval_every=_int(req, "eval_every", 100, 1, 10**6),
                             ckpt_every=_int(req, "ckpt_every", 200, 1, 10**6), early_stop_patience=_int(req, "patience", 0, 0, 1000))
            steps = tc.steps

            def on_event(e):
                if e["type"] in ("train", "val"):
                    self.event(rid, "metric", metric=e)
                    if e["type"] == "train":
                        self.event(rid, "progress", progress=int(100 * e["step"] / steps),
                                   message=f"step {e['step']}/{steps}  loss {e['loss']:.4f}")
            res = lab.train(_str(req, "model_id"), Path(_str(req, "dataset")), tc, confirmed=bool(req.get("confirmed")), cancel=cancel,
                            on_event=on_event, resume_run=_str(req, "resume_run", False))
            status = "cancelled" if res["status"] == "cancelled" else "success"
            self.out.send({"request_id": rid, "status": status, "result": res})
        except BadRequest as exc:
            self.error(rid, "INVALID_REQUEST", str(exc))
        except Exception as exc:
            log(traceback.format_exc())
            self.error(rid, *self.classify(exc))
        finally:
            with self.lock:
                self.job = None


def main() -> int:
    out = _Out()
    worker = LabWorker(out)
    log(f"model_lab worker started pid={os.getpid()}")
    out.send({"event": "ready", "request_id": None, "pid": os.getpid(), "worker": "model_lab"})
    lines: "queue.Queue[bytes | None]" = queue.Queue()

    def reader():
        while True:
            raw = sys.stdin.buffer.readline()
            lines.put(raw or None)
            if not raw:
                return

    threading.Thread(target=reader, daemon=True).start()
    while True:
        raw = lines.get()
        if raw is None:
            with worker.lock:
                if worker.job:
                    worker.job[1].set()
            import time
            for _ in range(100):                 # let a running job write its resumable checkpoint (max ~10 s)
                if worker.job is None:
                    break
                time.sleep(0.1)
            return 0
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            worker.error(None, "MALFORMED_REQUEST", "request is not valid UTF-8")
            continue
        if line:
            worker.handle(line)


if __name__ == "__main__":
    sys.exit(main())
