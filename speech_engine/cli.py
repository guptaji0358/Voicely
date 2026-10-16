"""Command line interface:  python -m speech_engine.cli --help

stdout carries results only (human text, or one JSON document with --json);
progress and notices go to stderr, so the CLI pipes cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path

import numpy as np

from .audio_pipeline import collect
from .backends.base import SynthParams
from .engine import SpeechEngine, make_backend
from .eval_corpus import CORPUS, LANG_FOR_ITEM
from .encoders import (INSTALL_HELP, encoder_status, parse_bitrate, resolve_format,
                       resolve_mp3_settings)
from .exceptions import EncoderUnavailableError, InvalidInputError, SpeechEngineError
from .voice_profiles import get_profile, load_profiles

EXIT_OK = 0


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _emit(args, payload: dict, human: str) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else human)


def _read_text(args) -> str:
    sources = [bool(args.text), bool(args.input)]
    if all(sources):
        raise InvalidInputError("use either --text or --input, not both")
    if args.text:
        return args.text
    if args.input:
        if args.input == "-":
            return sys.stdin.buffer.read().decode("utf-8")
        try:
            return Path(args.input).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise InvalidInputError(f"input file not found: {args.input}") from None
        except UnicodeDecodeError:
            raise InvalidInputError(f"{args.input} is not valid UTF-8") from None
        except OSError as exc:
            raise InvalidInputError(f"cannot read {args.input}: {exc}") from exc
    if not sys.stdin.isatty():
        return sys.stdin.buffer.read().decode("utf-8")
    raise InvalidInputError("no text given: use --text, --input FILE, or pipe text on stdin")


def _make_engine(args) -> SpeechEngine:
    backend = make_backend(args.backend)
    profile = get_profile(args.voice, args.profiles_file)
    profile = profile.with_overrides(speed=getattr(args, "speed", None))
    if backend.requires_network:
        print(f"notice: backend '{backend.name}' sends your text to a cloud service.", file=sys.stderr)
    return SpeechEngine(backend, profile)


def _progress_printer(args):
    if args.json or args.quiet:
        return None
    def show(done, total, text):
        print(f"\r[{done} chunks] {text[:50]!r:<56}", end="", file=sys.stderr, flush=True)
    return show


# ---------------------------------------------------------------- commands
def cmd_voices(args) -> int:
    profiles = load_profiles(args.profiles_file)
    out = {"profiles": {n: {"description": p.description, "local_blend": p.local_blend,
                            "google_voice": p.google_voice, "speed": p.speed}
                        for n, p in sorted(profiles.items())}}
    if args.backend:
        out["backend_voices"] = make_backend(args.backend).list_voices()
    lines = [f"{n:<10} {p.description}\n{'':<10} local blend={p.local_blend or '-'} | google voice={p.google_voice}"
             for n, p in sorted(profiles.items())]
    if "backend_voices" in out:
        lines.append(f"\n{args.backend} voices: {', '.join(out['backend_voices']) or '(unavailable)'}")
    _emit(args, out, "\n".join(lines))
    return 0


def cmd_test(args) -> int:
    results, ok_all = {}, True
    names = [args.backend] if args.backend else ["local", "google"]
    for n in names:
        st = make_backend(n).status()
        results[n] = {"available": st.available, "network": st.network,
                      "details": st.details, "problems": st.problems}
    # exit 0 if every *requested* backend is usable; unrequested ones are informational
    if args.backend:
        ok_all = results[args.backend]["available"]
    else:
        ok_all = any(r["available"] for r in results.values())
    results["encoders"] = encoder_status()
    if args.synth and ok_all:
        be = args.backend or next(n for n, r in results.items() if r["available"])
        args.backend, args.voice, args.speed = be, "original", None
        eng = _make_engine(args)
        t = time.perf_counter()
        a = collect(eng.iter_audio("This is a short test of the original voice."))
        results["synth"] = {"backend": be, "seconds": round(time.perf_counter() - t, 2),
                            "audio_s": round(a.duration, 2), "sample_rate": a.sample_rate}
    lines = []
    for n, r in results.items():
        if n == "synth":
            lines.append(f"synth test: {r}")
            continue
        if n == "encoders":
            lines.append(f"[mp3 export] {'OK (' + r['encoder'] + ')' if r['mp3'] else 'UNAVAILABLE'}")
            if r["problem"]:
                lines += [f"    ! {l}" for l in r["problem"].splitlines()]
            continue
        lines.append(f"[{n}] {'OK' if r['available'] else 'UNAVAILABLE'}")
        lines += [f"    {d}" for d in r["details"]] + [f"    ! {p}" for p in r["problems"]]
    _emit(args, {"ok": ok_all, "backends": results}, "\n".join(lines))
    return 0 if ok_all else 4


def _confirm_overwrite(path, force: bool) -> None:
    p = Path(path)
    if not p.exists() or force:
        return
    if p.is_dir():
        raise InvalidInputError(f"{p} is a directory")
    if sys.stdin.isatty() and not args_json_mode():
        if input(f"{p} exists. Overwrite? [y/N] ").strip().lower() in ("y", "yes"):
            return
        raise InvalidInputError("not overwriting existing file")
    raise InvalidInputError(f"{p} already exists; pass --force to overwrite")


def args_json_mode() -> bool:
    return "--json" in sys.argv


def cmd_speak(args) -> int:
    text = _read_text(args)
    fmt, bitrate = None, parse_bitrate(args.bitrate)
    if args.output:
        fmt = resolve_format(args.output, args.format)
        if bitrate is not None and fmt != "mp3":
            raise InvalidInputError("--bitrate only applies to MP3 output")
        if fmt == "mp3":
            resolve_mp3_settings(args.sample_rate, bitrate)  # validate before any synthesis
            if not encoder_status()["mp3"]:
                raise EncoderUnavailableError(INSTALL_HELP)
        _confirm_overwrite(args.output, args.force)
    elif args.format or bitrate is not None:
        raise InvalidInputError("--format/--bitrate need --output")
    eng = _make_engine(args)
    st = eng.backend.status()
    if not st.available:
        raise _unavailable(st)
    params = SynthParams(speed=args.speed, style=args.style, timeout=args.timeout)
    prog = _progress_printer(args)
    try:
        if args.output:
            info = eng.synthesize_to_file(text, args.output, fmt, params,
                                          sample_rate=args.sample_rate, progress=prog,
                                          bitrate_kbps=bitrate)
            info.update(backend=args.backend, voice=args.voice, played=False)
        else:
            if args.no_play:
                raise InvalidInputError("--no-play needs --output")
            eng.speak(text, params, prog)
            info = {"backend": args.backend, "voice": args.voice, "played": True}
    finally:
        if prog:
            print(file=sys.stderr)
        eng.close()
    _emit(args, {"ok": True, **info}, f"done: {info}")
    return 0


def _unavailable(st):
    from .exceptions import BackendUnavailableError
    return BackendUnavailableError(f"backend '{st.name}' unavailable: " + "; ".join(st.problems))


def cmd_download_model(args) -> int:
    from .backends.local_backend import MODEL_FILES, default_model_dir, download_model
    which = "kokoro-v1.0.int8.onnx" if args.int8 else "kokoro-v1.0.onnx"
    d = Path(args.model_dir) if args.model_dir else default_model_dir()
    sizes = f"{which} {MODEL_FILES[which]} + voices-v1.0.bin {MODEL_FILES['voices-v1.0.bin']}"
    if not args.yes:
        print(f"This would download from github.com/thewh1teagle/kokoro-onnx releases:\n  {sizes}\n"
              f"into {d}\nLicense: Kokoro weights Apache-2.0. Re-run with --yes to proceed.", file=sys.stderr)
        return 2
    def prog(name, got, total):
        print(f"\r{name}: {got >> 20}/{total >> 20 if total else '?'} MB", end="", file=sys.stderr, flush=True)
    paths = download_model(which, d, prog)
    print(file=sys.stderr)
    _emit(args, {"ok": True, "files": [str(p) for p in paths]}, "\n".join(map(str, paths)))
    return 0


def _metrics(pcm: bytes, rate: int) -> dict:
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768.0
    if x.size == 0:
        return {"duration_s": 0.0}
    win = max(1, rate // 50)
    frames = x[: x.size // win * win].reshape(-1, win)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    jumps = np.abs(np.diff(x))
    return {
        "duration_s": round(x.size / rate, 3),
        "peak": round(float(np.abs(x).max()), 3),
        "clipped_fraction": round(float((np.abs(x) > 0.999).mean()), 6),
        "silence_fraction": round(float((rms < 0.005).mean()), 3),
        # crude click/discontinuity heuristic; NOT a perceptual quality score
        "click_candidates": int((jumps > 0.5).sum()),
    }


def cmd_evaluate(args) -> int:
    """Run the fixed corpus through one or more voice profiles for A/B comparison.

    Reports objective numbers only. Intelligibility, naturalness, prosody and
    voice consistency need human listening: use the rating sheet from --rating-sheet.
    """
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    items = {k: v for k, v in CORPUS.items() if args.multilingual or k not in LANG_FOR_ITEM}
    if args.items:
        items = {k: items[k] for k in args.items.split(",") if k in items}
    be = make_backend(args.backend)
    st = be.status()
    if not st.available:
        raise _unavailable(st)
    outdir = Path(args.output_dir) if args.output_dir else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    rows = []
    for vname in voices:
        base = get_profile(vname, args.profiles_file)
        for key, text in items.items():
            prof = replace(base, local_lang=LANG_FOR_ITEM.get(key, base.local_lang))
            eng = SpeechEngine(be, prof)
            tracemalloc.start()
            t0 = time.perf_counter()
            first = None
            parts, rate = [], None
            for c in eng.iter_audio(text):
                if first is None:
                    first = time.perf_counter() - t0
                parts.append(c.pcm); rate = c.sample_rate
            wall = time.perf_counter() - t0
            peak_mem = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            pcm = b"".join(parts)
            m = _metrics(pcm, rate)
            m.update(voice=vname, item=key, backend=args.backend,
                     first_chunk_latency_s=round(first or 0, 3),
                     total_s=round(wall, 3),
                     real_time_factor=round(wall / m["duration_s"], 3) if m["duration_s"] else None,
                     py_heap_peak_mb=round(peak_mem / 1e6, 1))
            rows.append(m)
            if outdir:
                from .audio_pipeline import WavFileSink
                from .backends.base import AudioChunk
                s = WavFileSink(outdir / f"{vname}__{key}.wav"); s.write(AudioChunk(pcm, rate)); s.close()
            print(f"  {vname:<10} {key:<16} {m['duration_s']:>6.2f}s rtf={m['real_time_factor']}", file=sys.stderr)
    if args.rating_sheet:
        lines = ["voice,item,intelligibility_1-5,pronunciation_1-5,naturalness_1-5,prosody_1-5,consistency_1-5,artifacts_noted"]
        lines += [f"{r['voice']},{r['item']},,,,,," for r in rows]
        Path(args.rating_sheet).write_text("\n".join(lines) + "\n", encoding="utf-8")
    note = ("Objective metrics only. py_heap_peak_mb covers Python allocations, not native ONNX memory. "
            "Naturalness/prosody/consistency require human listening (see --rating-sheet).")
    _emit(args, {"ok": True, "note": note, "results": rows},
          "\n".join(f"{r['voice']:<10}{r['item']:<16}{r['duration_s']:>7}s rtf={r['real_time_factor']} "
                    f"peak={r['peak']} clicks={r['click_candidates']}" for r in rows) + "\n" + note)
    return 0


# ---------------------------------------------------------------- parser
def _add_backend(p, default="local", help_extra=""):
    p.add_argument("--backend", choices=["local", "google"], default=default,
                   help="synthesis backend (default: local/offline; env SPEECH_ENGINE_BACKEND). "
                        "'google' sends text to Google." + help_extra)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m speech_engine.cli",
                                description="Original speech engine (local Kokoro, optional Google Gemini TTS).")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profiles-file", help="JSON file with extra/overriding voice profiles")
    common.add_argument("--json", action="store_true", help="machine-readable JSON on stdout")
    common.add_argument("--quiet", "-q", action="store_true", help="no progress output")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("voices", parents=[common], help="list voice profiles")
    v.set_defaults(fn=cmd_voices)
    _add_backend(v, None, " For `voices`: also list this backend's raw voices.")

    t = sub.add_parser("test", parents=[common], help="check configuration and backend availability")
    t.add_argument("--synth", action="store_true", help="also synthesize a short phrase (google: uses quota)")
    t.set_defaults(fn=cmd_test)
    _add_backend(t, None, " Default for `test`: check all.")

    s = sub.add_parser("speak", parents=[common], help="synthesize and play, or save")
    s.add_argument("--text", "-t")
    s.add_argument("--input", "-i", help="UTF-8 text file, or '-' for stdin")
    s.add_argument("--voice", default="original", help="voice profile (default: original)")
    s.add_argument("--speed", type=float, help="speaking-rate multiplier 0.5-2.0 (profile default otherwise)")
    s.add_argument("--style", help="google only: delivery instruction overriding the profile's style")
    s.add_argument("--output", "-o", help="write audio here instead of playing")
    s.add_argument("--format", choices=["wav", "mp3", "pcm"], default=None,
                   help="output format; inferred from the --output extension when omitted "
                        "(an explicit value must match a .wav/.mp3/.pcm extension)")
    s.add_argument("--bitrate", help="MP3 only, e.g. 128k, 192k (default), 320k; valid values depend on sample rate")
    s.add_argument("--force", action="store_true", help="overwrite an existing output file without asking")
    s.add_argument("--sample-rate", type=int, help="output sample rate (MP3 default: 44100)")
    s.add_argument("--no-play", action="store_true", help="require --output; do not play")
    s.add_argument("--timeout", type=float, default=60.0, help="per-request timeout, cloud backend")
    s.set_defaults(fn=cmd_speak)
    _add_backend(s, os.environ.get("SPEECH_ENGINE_BACKEND", "local"))

    e = sub.add_parser("evaluate", parents=[common], help="run the fixed test corpus; compare voices with --voices")
    e.add_argument("--voices", default="original", help="comma list, e.g. original,reference,deep")
    e.add_argument("--items", help="comma list of corpus item names")
    e.add_argument("--multilingual", action="store_true", help="include fr/es/hi items")
    e.add_argument("--output-dir", help="save each rendering as WAV for listening")
    e.add_argument("--rating-sheet", help="write a CSV template for human scoring")
    e.set_defaults(fn=cmd_evaluate)
    _add_backend(e, os.environ.get("SPEECH_ENGINE_BACKEND", "local"))

    d = sub.add_parser("download-model", parents=[common], help="download Kokoro model files (explicit consent)")
    d.add_argument("--yes", action="store_true", help="confirm the download")
    d.add_argument("--int8", action="store_true", help="smaller quantised model (~80 MB)")
    d.add_argument("--model-dir")
    d.set_defaults(fn=cmd_download_model)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except SpeechEngineError as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc), "exit_code": exc.exit_code}))
        _err(str(exc))
        return exc.exit_code
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
