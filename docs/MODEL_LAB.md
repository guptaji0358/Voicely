# Model Lab: a from-scratch, growable language model

An experimental, **optional** subsystem. It trains a small decoder-only Transformer from random weights on a narrow task that
matters to the speech app, evaluates it honestly against a rule baseline, and supports controlled architecture expansion.
It never downloads or loads a pretrained model, and the speech application does not import it.

## What exists
| Piece | File | Notes |
|---|---|---|
| Architecture + exact parameter formula | `model_lab/config.py`, `model.py` | pre-LN decoder, learned positions, causal attention, GELU MLP, tied embeddings; `head_dim` is independent so heads can grow |
| Tokenizer learned from your data | `tokenizer.py` | characters, optional BPE merges; nothing pretrained |
| Versioned datasets | `data.py` | immutable `vNNNN-<hash>` folders, SHA-256 manifest, dedup, contradiction exclusion, hash-based train/val/test split, separate held-out regression split, memory-mapped token arrays |
| Training | `train.py` | AdamW, warm-up + cosine, gradient clipping, gradient accumulation, periodic atomic checkpoints, resume (model + optimiser + RNG), cancel, early stopping, non-finite detection, first-step gradient/weight-change check |
| Evaluation + acceptance | `evaluate.py` | label accuracy by real greedy generation, macro-F1, loss/perplexity over label tokens, rule baseline and majority-class rate beside every result |
| Registry + promotion | `registry.py` | `random_init`, `expanded`, `trained`, `eval_passed`, `eval_failed`, `promoted`, `superseded`; promotion needs `eval_passed` **and** explicit approval; rollback keeps history |
| Expansion | `expand.py` | depth / FFN width / heads / sequence length are function-preserving; `d_model` growth is not and is reported as such |
| Memory budget | `memory.py` | estimate before allocating, safety margin (`MODEL_LAB_SAFETY_MARGIN_GB`, default 1 GB), hard floor, ok / needs_confirmation / refuse, measured peak RSS |
| Orchestration, CLI, worker | `lab.py`, `cli.py`, `worker.py` | the worker is a separate process speaking the existing JSON-lines protocol |
| Qt UI | `gui/src/ui/ModelLabDialog.*` | Tools menu > Model Lab (Ctrl+M) |

## The task, and where the data comes from
**Task:** given a text segment, name the pronunciation-risk category it contains: `plain, number, date, unit, abbreviation, acronym, heteronym`.
It is narrow, measurable, and has reliable labels.

**Data provenance (important):** the only data bundled is *synthetic*. `model_lab/synth.py` builds segments from neutral filler words
plus exactly one trigger of a known category, so labels are correct by construction. They are not user data, not production data,
and not produced by any model. The held-out *regression* split uses trigger words that never occur in train/val/test.
Real approved corrections from `speech_engine/feedback.py` can be added with `data.import_feedback_records` (only corrections whose
recheck succeeded and whose category maps to a label); that path is tested but there is no real user data in this repository.

**Baseline:** `baseline.py` applies the application's own rule lists (`speech_engine.text_analysis`). A learned model must be compared with it.

## What the model can and cannot be expected to do
A model of 10 K to 1 M parameters trained on thousands of templated examples can learn a closed classification task. It cannot do
open-ended text correction, phonetics, or anything that needs world knowledge. That needs a much larger model and far more data, and
should stay a separate, modular component (the optional LLM layer in `speech_engine/llm/`). Nothing here claims otherwise.

## Real results on this machine (i5-12450H, 16 GB RAM, CPU only, PyTorch 2.13)
Produced by `python -m model_lab.experiments`; the raw report is `lab_reports/experiment_report.json`. Dataset `v0001-fa4f3045`
(synthetic; 2,800 train / 356 val / 341 test / 434 held-out regression). 1,500 steps, batch 32, all from random init (seed 11).

| Size (actual params) | Train time | Peak RSS (est.) | Val acc | Test acc | Held-out regression acc | Outcome |
|---|---|---|---|---|---|---|
| 1K (928) | 13 s | 307 MB (328) | 0.3% | 0.6% | 0.9% | **failed**: accuracy far below the majority-class rate (928 parameters cannot hold the task) |
| 10K (10,143) | 41 s | 328 MB (352) | 79.2% | 79.8% | 64.5% | passed the gates (first model, so no best to beat) |
| 100K (99,282) | 67 s | 366 MB (384) | 82.6% | 80.1% | 65.9% | **failed**: val loss not within 5% of the best |
| 1M (1,001,891) | 500 s | 516 MB (497) | 82.6% | 78.6% | 63.6% | **failed**: same gate |
| Rule baseline | - | - | **88.5%** | - | 66.8% | - |
| Majority class | - | - | 16.6% | - | - | - |

Honest reading: the learned models clearly learn (random init scores 0%), but **none beats the rule baseline in-distribution**, and on
unseen trigger words they are roughly equal to it (63.6-65.9% vs 66.8%). Larger models did not help here: the data, not capacity, is
the limit. The current best checkpoint is the 10 K model (`eval_passed`, **not promoted**: promotion needs your explicit approval).
The 100 K model has higher accuracy but higher validation loss (it is more over-confident), so the acceptance gate rejected it. That is the
gate working as designed, not a tuned-to-pass result.

**Expansion test (100 K -> 1 M):** layers 3->4, d_model 50->145, heads 3->8, FFN 200->580. Because `d_model` changed it is *not*
function-preserving: measured max logit difference 27.8, top-1 agreement 62%, validation accuracy 4% before training. It was blocked from
acceptance until trained; after 600 more steps it reached 82.6% val / 67.3% regression accuracy (similar to the 1 M model trained from
scratch for 1,500 steps, but this does not show expansion is better, only that it recovers). Function-preserving expansions (more layers,
wider FFN, more heads, longer context) are verified by unit tests to reproduce the old logits within 1e-5.

**Sizes actually trained:** 1K, 10K, 100K, 1M (plus the expanded ~1 M). **Not trained:** 10M and 100M (feasible in principle; 100M was
estimated at 2.4 GB and allowed by the guard, but was not attempted), **1B (18 GB) and 8B (129 GB) are refused by the memory guard** and
exist only as architecture descriptions. No 1B or 8B checkpoint was created.

## Commands
```bash
python -m model_lab info
python -m model_lab make-data --n-per-label 500
python -m model_lab init --name demo --dataset <dataset path> --size 100K --seed 1
python -m model_lab train --model <id> --dataset <dataset path> --steps 1500        # Ctrl+C = stop with a resumable checkpoint
python -m model_lab eval --model <id> --dataset <dataset path>
python -m model_lab expand --checkpoint <id> --target-parameters 1000000            # NEW checkpoint, source untouched
python -m model_lab promote --model <id> --approve                                   # needs eval_passed AND --approve
python -m model_lab rollback
python -m model_lab estimate --size 1B --batch-size 8                                # allocates nothing
python -m model_lab.experiments --root <folder> --out report.json                    # the reproducible experiment
```
Lab data lives in `%LOCALAPPDATA%\speech_engine\model_lab` (override `MODEL_LAB_DIR`): `registry.json`, `models/<name>/*.pt`, `datasets/`, `runs/`.

## Expansion rules
Never overwrites the source. Records every tensor as copied / expanded / identity-initialised / randomly initialised. Shrinking, changing
`vocab_size` or `head_dim` is rejected. **Optimizer state is not carried over** (shapes change); training restarts AdamW moments from zero.
Expansion adds capacity, not knowledge; an expanded model must be trained and pass evaluation before it can be accepted.

## Pause / cancel
*Pause* and *Cancel* stop at the next step. The latest resumable checkpoint is written atomically (temp file + rename), so a cancel or
crash cannot corrupt an existing checkpoint. *Resume* restores model, optimiser and RNG state; the test suite checks that cancel + resume
reproduces an uninterrupted run.

## Packaging
`python packaging/make_dist.py --with-lab` adds only `engine/model_lab` (about 150 KB). PyTorch is **not** bundled unless `--with-torch`
(hundreds of MB); without it the Model Lab window explains that PyTorch is missing and asks for a Python that has it (Settings or
`TTS_LAB_PYTHON`). A Model Lab packaging failure only drops the feature; the base build continues. Datasets and checkpoints are never bundled.

## Future work (not done)
Real user-approved data at scale; a held-out human-labelled test set; GPU or external compute for models above ~100 M parameters;
a fine-tuning workflow only for a model and licence that allow it (see `docs/LEARNING.md`).
