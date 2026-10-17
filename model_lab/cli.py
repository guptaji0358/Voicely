"""model-lab command line:   python -m model_lab <command> ...      (all output is JSON on stdout)

  info                                   hardware, memory and registry summary
  make-data   [--n-per-label N]          create a synthetic dataset version
  datasets                               list dataset versions
  init        --name --dataset --size 100K [--seed]      randomly initialised model (no pretrained weights)
  train       --model ID --dataset PATH --steps N ...    train (creates a NEW checkpoint/registry entry)
  eval        --model ID --dataset PATH                  evaluate against val/test/regression and the rule baseline
  expand      --checkpoint ID --target-parameters N      grow a model into a NEW checkpoint (source untouched)
  promote     --model ID --approve                       needs eval_passed AND --approve
  rollback                                              restore the previous production model
  list                                                  registry
  estimate    --size 1B --batch-size 8                   memory estimate and budget decision (allocates nothing)
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading

from . import memory
from .config import SIZE_TARGETS, preset
from .lab import Lab, LabError
from .train import TrainConfig


def _out(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="model-lab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="lab folder (default: %%LOCALAPPDATA%%\\speech_engine\\model_lab or MODEL_LAB_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info")
    p = sub.add_parser("make-data"); p.add_argument("--name", default="review-category"); p.add_argument("--n-per-label", type=int, default=400); p.add_argument("--seed", type=int, default=7)
    sub.add_parser("datasets")
    p = sub.add_parser("init"); p.add_argument("--name", required=True); p.add_argument("--dataset", required=True)
    p.add_argument("--size", default="100K", choices=list(SIZE_TARGETS)); p.add_argument("--seed", type=int, default=0)
    p.add_argument("--max-seq", type=int, default=96); p.add_argument("--confirm", action="store_true")
    p = sub.add_parser("train"); p.add_argument("--model", required=True); p.add_argument("--dataset", required=True)
    p.add_argument("--steps", type=int, default=1000); p.add_argument("--batch-size", type=int, default=32); p.add_argument("--grad-accum", type=int, default=1)
    p.add_argument("--lr", type=float, default=3e-3); p.add_argument("--seed", type=int, default=1234); p.add_argument("--eval-every", type=int, default=100)
    p.add_argument("--ckpt-every", type=int, default=200); p.add_argument("--patience", type=int, default=0); p.add_argument("--resume-run")
    p.add_argument("--confirm", action="store_true")
    p = sub.add_parser("eval"); p.add_argument("--model", required=True); p.add_argument("--dataset", required=True)
    p = sub.add_parser("expand"); p.add_argument("--checkpoint", required=True, help="registry model id"); p.add_argument("--target-parameters", type=int, required=True)
    p.add_argument("--seed", type=int, default=0); p.add_argument("--confirm", action="store_true")
    p = sub.add_parser("promote"); p.add_argument("--model", required=True); p.add_argument("--approve", action="store_true"); p.add_argument("--approver", default="")
    sub.add_parser("rollback")
    sub.add_parser("list")
    p = sub.add_parser("estimate"); p.add_argument("--size", required=True, choices=list(SIZE_TARGETS)); p.add_argument("--batch-size", type=int, default=8)
    p.add_argument("--vocab", type=int, default=80); p.add_argument("--max-seq", type=int, default=96)
    a = ap.parse_args(argv)

    from pathlib import Path
    lab = Lab(Path(a.root) if a.root else None)
    try:
        if a.cmd == "info":
            _out({"hardware": memory.hardware_report(), "registry": lab.registry.status(), "root": str(lab.root)})
        elif a.cmd == "make-data":
            _out(lab.make_synthetic_dataset(a.name, a.n_per_label, a.seed))
        elif a.cmd == "datasets":
            _out(lab.datasets())
        elif a.cmd == "init":
            _out(lab.init_model(a.name, Path(a.dataset), size=a.size, max_seq=a.max_seq, seed=a.seed, confirmed=a.confirm))
        elif a.cmd == "train":
            cancel = threading.Event()
            signal.signal(signal.SIGINT, lambda *_: cancel.set())      # Ctrl+C: stop at the next step, keep a resumable checkpoint
            tc = TrainConfig(steps=a.steps, batch_size=a.batch_size, grad_accum=a.grad_accum, lr=a.lr, seed=a.seed,
                             eval_every=a.eval_every, ckpt_every=a.ckpt_every, early_stop_patience=a.patience)
            res = lab.train(a.model, Path(a.dataset), tc, confirmed=a.confirm, cancel=cancel,
                            on_event=lambda e: print(json.dumps(e), file=sys.stderr, flush=True), resume_run=a.resume_run)
            _out(res)
        elif a.cmd == "eval":
            _out(lab.evaluate(a.model, Path(a.dataset)))
        elif a.cmd == "expand":
            _out(lab.expand(a.checkpoint, a.target_parameters, a.seed, a.confirm))
        elif a.cmd == "promote":
            _out(lab.promote(a.model, a.approve, a.approver))
        elif a.cmd == "rollback":
            _out(lab.rollback())
        elif a.cmd == "list":
            _out({"models": lab.registry.models(), **lab.registry.status()})
        elif a.cmd == "estimate":
            cfg = preset(a.size, a.vocab, a.max_seq)
            est = memory.estimate_training_bytes(cfg, a.batch_size)
            _out({"size": a.size, "config": cfg.to_dict(), **est, "decision": memory.check_budget(est["total"]).to_dict()})
    except Exception as exc:          # structured error, non-zero exit
        _out({"error": type(exc).__name__, "message": str(exc)})
        return 2 if isinstance(exc, (LabError,)) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
