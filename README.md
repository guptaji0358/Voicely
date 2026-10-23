# Speech engine (`speech_engine/`)

Reads extracted text aloud with an original voice profile, `original`. Independent of the OCR
code; wired into the chat as `:speak` / `:stop`.

## What it is (and is not)
* **Backends:** `local` (default) — Kokoro-82M via `kokoro-onnx`, offline, CPU, audio stays in RAM.
  `google` — Gemini speech generation, opt-in, **sends text to Google**.
* **`original` is a prompt/config-defined voice profile, not a trained acoustic model.**
  - local: weighted blend of Kokoro voice style vectors (`af_heart` .5, `bf_emma` .3, `af_nicole` .2) + speed/pause settings. Blending yields a timbre that is not a single stock voice, but it is derived from Kokoro's voices.
  - google: prebuilt voice `Sulafat` + a natural-language style instruction. The underlying voice is Google's.
  - The blend weights/style text are *design starting points*; they have not been auditioned by the author of this code. Tune by ear (below).
* No Windows SAPI / system voices anywhere. If a backend fails, you get an error — never a different voice.
* A truly custom-trained voice: extension point documented in `speech_engine/backends/base.py` (needs licensed, consented dataset, GPU training, evaluation). Not implemented.

## Install
```bash
pip install -r requirements.txt            # or just: pip install numpy sounddevice kokoro-onnx
python -m speech_engine.cli download-model --yes     # ~310 MB fp32 (+~27 MB voices); --int8 for ~80 MB
```
Model files go to `%LOCALAPPDATA%\speech_engine\models` (override: `SPEECH_ENGINE_MODEL_DIR`). Sources: github.com/thewh1teagle/kokoro-onnx releases (`model-files-v1.0`). Sizes are approximate. Licenses: kokoro-onnx MIT, Kokoro weights Apache-2.0 (verify upstream before redistributing). CPU only needed; a few hundred MB RAM.

Google (optional): `pip install google-genai`, then set `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in the environment. Model: `SPEECH_ENGINE_GOOGLE_MODEL` (default `gemini-3.8-flash-tts`, from Google's docs at time of writing — check them, models change).

## CLI
```bash
python -m speech_engine.cli --help
python -m speech_engine.cli voices [--backend local]
python -m speech_engine.cli test [--backend local|google] [--synth]
python -m speech_engine.cli speak --text "Hello world" --voice original
python -m speech_engine.cli speak --input document.txt --voice original
python -m speech_engine.cli speak --input document.txt -o speech.wav [--format wav|pcm] [--sample-rate 16000]
type document.txt | python -m speech_engine.cli speak            # stdin
python -m speech_engine.cli speak --backend google --text "Hi" --style "warm, unhurried"
python -m speech_engine.cli evaluate --voices original,reference,deep --output-dir out --rating-sheet rate.csv
python -m speech_engine.cli download-model --yes
```
`--json` prints one JSON object on stdout (progress/notices go to stderr; `-q` silences progress).
Exit codes: 0 ok, 2 bad input, 3 configuration, 4 backend unavailable, 5 synthesis failed, 6 rate limit, 7 audio output, 8 encoder missing, 130 interrupted.
Output formats: `wav`, `mp3` (see *Save as MP3*) and raw `pcm`.


## Save as MP3
```bash
python -m speech_engine.cli speak --text "Hello world" --voice original --output speech.mp3
python -m speech_engine.cli speak --input document.txt --voice original --format mp3 --bitrate 192k --output narration.mp3
```
* **Format** is inferred from the extension (`.wav`, `.mp3`, `.pcm`). An explicit `--format` overrides inference but must agree with a known extension (`--format mp3 -o a.wav` is an error). Unknown extension with no `--format` is an error.
* **Encoder:** `lameenc` (LAME, bundled in the wheel): `pip install lameenc`. No FFmpeg required. It is a genuine MPEG-1/2 Layer III encode of the PCM - never a renamed WAV. Check with `python -m speech_engine.cli test` (shows `[mp3 export] OK`).
* **Bitrate:** `--bitrate 192k` (default). Valid values depend on the sample rate: 32-48 kHz -> 32,40,48,56,64,80,96,112,128,160,192,224,256,320k; 16/22.05/24 kHz -> 8...160k; 8/11.025/12 kHz -> 8...64k. An invalid pair is rejected before any synthesis. CBR only.
* **Sample rate:** MP3 output defaults to 44.1 kHz mono (synthesis at 24 kHz is resampled, polyphase when scipy is installed, linear otherwise). Override with `--sample-rate`. Output is mono; it is never converted to fake stereo.
* **Long documents:** text is chunked, synthesized in order and encoded as a stream (bounded memory, no intermediate WAV) with short fades at chunk joins.
* **Safe writes:** audio goes to a hidden `.name.PID.part` file beside the target and is renamed only after the MP3 frames are validated (every frame header walked, duration cross-checked). On any failure or Ctrl+C the temp file is deleted, an existing file at the target is left untouched, and the CLI exits non-zero without a success message. Existing outputs are never overwritten silently: interactive runs ask, scripts need `--force`.
* **Cloud MP3 passthrough:** not implemented because no supported provider path returns MP3 (Gemini TTS returns WAV/PCM). Such audio is decoded and encoded; compressed bytes are never played or written as PCM.
* **Other formats:** WAV (16-bit mono) and raw PCM remain available. Other formats (FLAC, OGG, AAC) are not supported and are rejected with a clear message.
* **Chat app:** in Image Text Extracter, `:mp3` opens a native Save dialog (default `.mp3`, overwrite prompt); `:mp3 voice=boy C:\out.mp3` skips it. Export runs on a background thread, prints progress, and prints "SAVED" only after validation.

### MP3 troubleshooting
| Symptom | Fix |
|---|---|
| `MP3 export needs the 'lameenc' package` (exit 8) | `pip install lameenc` (use the same Python as `python -m speech_engine.cli`) |
| `not a valid MP3 bitrate at N Hz` | pick a listed bitrate or change `--sample-rate` |
| `insufficient disk space` | free space on the target drive; nothing partial is left behind |
| `already exists; pass --force` | choose another name or add `--force` |
| Player shows no duration/seek | CBR file without a Xing/Info tag; playback is normal, some players estimate length from bitrate |

## Python API
```python
from speech_engine import SpeechEngine, make_backend, get_profile
eng = SpeechEngine(make_backend("local"), get_profile("original"))
h = eng.speak_async(text)   # non-blocking; a new call interrupts the previous one
h.cancel(); eng.close()
```

## Voice library and stored previews

24 built-in voices: 4 classic (`original`, `reference`, `deep`, `boy`), 10 girls (`girl_*`) and 10 boys (`boy_*`), grouped in the dropdown. Metadata (`gender`, `display_name`, `tagline`) lives on each profile in `speech_engine/voice_profiles.py`.
The speaker button plays a stored clip from `speech_engine/voice_samples/<voice>.mp3` (any backend, never a cloud call) instead of synthesizing. After changing a profile, regenerate with `python packaging/make_voice_samples.py --force --only <voice>`.

## Tuning the voice
Copy a profile into a JSON file and pass `--profiles-file` (or `SPEECH_ENGINE_PROFILES`):
`{"original": {"local_blend": {"af_heart": 0.6, "bf_emma": 0.4}, "speed": 0.95}}`. Run `evaluate --output-dir` and listen.
Kokoro's non-English voices differ per language; English blends reading French/Hindi will have an accent — use language-specific voices in a custom profile.

## Evaluation
`evaluate` runs a fixed corpus (conversational, questions, numbers/dates, abbreviations, OCR text, long paragraph, optional fr/es/hi) and reports **objective** numbers only: duration, latency, real-time factor, peak, clipping, silence fraction, click candidates, Python-heap peak. Intelligibility, naturalness, prosody and consistency need human listening — `--rating-sheet` writes a CSV to fill in.

## Privacy / memory
Local backend: no network; text and audio never leave the machine. Google backend: text is sent to Google, only when selected. Playback is RAM-only. Files are written only with `-o` (atomically). Long text is chunked (≤350/1500 chars) and streamed; WAV is written incrementally.

## Troubleshooting
* `test` shows what is missing. `kokoro-onnx not installed` → pip install. `missing model` → `download-model --yes`.
* No sound → check default output device; `python -c "import sounddevice; print(sounddevice.query_devices())"`.
* HTTP 403/400 from Google → key/model wrong (reported without printing the key). 429 → quota; retry later.

---

# Desktop GUI (C++ / Qt 6) — `gui/`

The visible app is native C++/Qt Widgets; Python stays the engine. `gui/src/ProtocolClient.*` launches
`python -m speech_engine.worker` through `QProcess` and talks JSON-lines ([docs/PROTOCOL.md](docs/PROTOCOL.md));
`speech_engine/worker.py` is a thin adapter over the same `SpeechEngine` the CLI uses (no duplicated synthesis code).
Flow: **Generate Speech** -> temporary WAV preview (played with Qt Multimedia) -> **Save as MP3** re-encodes that WAV with LAME
(no re-synthesis) after a native save dialog. Settings (Python path, engine folder, session-only Google key) live in the app.

## Why the old CLI "failed" (diagnosed, reproduced)
The engine itself works here (`test`, `speak -o x.mp3`, playback and 55 pytest tests passed before any change). What fails is the
launch environment: `python -m speech_engine.cli` only works when the current directory is the repo root
(`No module named 'speech_engine'` anywhere else), and the `py` launcher's default is a non-existent `python3.15t.exe`
(`Unable to create process using ...python3.15t.exe`). The GUI sets `PYTHONPATH`/cwd itself and uses an explicit interpreter.

## Build (Windows, Qt 6.11.1 MinGW 13.1 kit, CMake + Ninja)
```bash
export PATH="/d/Apps/Qt6/Tools/mingw1310_64/bin:/d/Apps/Qt6/6.11.1/mingw_64/bin:/d/Apps/Qt6/Tools/CMake_64/bin:/d/Apps/Qt6/Tools/Ninja:$PATH"
cd gui
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH=D:/Apps/Qt6/6.11.1/mingw_64 -DCMAKE_CXX_COMPILER=D:/Apps/Qt6/Tools/mingw1310_64/bin/g++.exe -DPython3_EXECUTABLE=D:/Apps/python/python.exe
cmake --build build
cd build && ctest --output-on-failure     # protocol + GUI tests (mock backend; no key/model needed, one test uses real Kokoro if installed)
```
Adjust the Qt/compiler paths to your install (use Qt's own MinGW, not an unrelated g++). Python tests: `python -m pytest`.

## Launch
Double-click `gui/build/Voicely.exe` (needs the Qt `bin` folder on PATH, or run `cmake --build build --target deploy` once to copy the Qt DLLs/plugins next to it).
Python is found via Settings > `TTS_PYTHON` > `<app>/python/python.exe` > PATH; the engine via Settings > `TTS_ENGINE_ROOT` > a `speech_engine` folder at/above or in `engine/` beside the exe. Python needs `pip install -r requirements.txt` (+ `python -m speech_engine.cli download-model --yes` for the local voice).

## Packaging plan
1. `cmake --build build --target deploy` (windeployqt). 2. Copy `speech_engine/` to `<app>/engine/`. 3. Bundle an embeddable CPython 3.13 into `<app>/python/` with `pip install -r requirements.txt --target <app>/python/Lib/site-packages` (enable `import site` in `python313._pth`). 4. Keep the ~310 MB Kokoro model out of the installer: first-run download via `download-model --yes` into `%LOCALAPPDATA%\speech_engine\models` (or ship the 80 MB int8 build). 5. Wrap in an installer (Inno Setup/NSIS/WiX). Not done or tested here.

## Limitations
Google backend untested (no API key here; only its "unavailable" error path is tested). Non-English languages use English-blend voices (accent). Only WAV/MP3 export. Qt Multimedia FFmpeg backend. The packaging steps above are a plan, not verified.

## Desktop GUI (`gui/`)
Qt 6 Widgets app (C++) talking to the Python worker over the JSON-lines protocol (`docs/PROTOCOL.md`, versions 1 and 2).

```
gui/assets/styles/    dark.qss (colour tokens) + components.qss (rules, layout tokens)
gui/assets/icons/     24x24 stroke SVGs (currentColor, recoloured per theme at paint time)
gui/assets/illustrations/  empty-state SVGs
gui/resources/resources.qrc  -> :/styles/*, :/icons/*, :/illustrations/*
gui/src/ui/           ThemeManager, Icons, Buttons (animated hover/press, PreviewButton), Indicators
```
* **Themes:** a theme file is just `@token: value;` lines. Copy `dark.qss`, add the name to `ThemeManager::availableThemes()`. `components.qss` references tokens as `@name`. If a stylesheet fails to load, a built-in fallback style is applied and the reason is shown in the status line. `TTS_STYLE_DIR=<folder>` loads the `.qss` files from disk for live editing.
* **Animations** use `QVariantAnimation` / `QPropertyAnimation` (QSS cannot animate). They stop when hidden and are disabled by `TTS_REDUCE_MOTION=1`, Settings > Reduce motion, or the Windows "Show animations" setting.
* **Waveform** is driven by real decoded samples from `QAudioBufferOutput`; it stays still if none arrive.
* **Voice preview** is the speaker button inside the voice row; it uses its own player so the generated audio is not replaced.

### Pronunciation QA, dictionary and LLM assistance
Workflow: import/enter text -> *Check pronunciation* (pre-analysis, optional) -> choose voice -> *Generate Speech* -> listen -> *Recheck Speech*
(optional) -> inspect suspected problems (intended vs recognised text, replay) -> *Correct…* / *Repair* / *Auto-repair* -> listen to the repaired
version, *Accept* or keep the original -> recheck -> save MP3/WAV. Details and honest limits: `docs/PRONUNCIATION_QA.md`.
AI suggestions and learning from corrections (off by default, consent-gated, nothing trained automatically): `docs/LEARNING.md`.
Recheck Speech needs `pip install faster-whisper` plus a recognition model (Settings > Pronunciation > Download, asks first).

### Window, layout and effects
Custom title bar (`Qt::FramelessWindowHint`; moving/resizing/snapping are done by the OS via `startSystemMove/startSystemResize`), floating cards
with painted layered shadows over a subtle perspective backdrop, staggered entrance fades, animated hover/press, waveform from real decoded
samples. It is a genuine native widget app; depth is created with layered 2D painting (no 3D engine). Fallback: Settings > "Use the standard Windows
title bar" or `TTS_NATIVE_FRAME=1`. Reduced motion: Settings or `TTS_REDUCE_MOTION=1` (also follows the Windows animation setting).
Import / Paste / Clear / Check-pronunciation, Settings, the side-panel toggle and the command menu are icon buttons in the title bar (tooltips
name them). Every dialog (Settings, Voice library, Dictionary, Review, Model Lab, message boxes) uses the same frameless, shadowed window with
the same backdrop and its own title bar (`ui/DialogFrame.*`); menus are drawn by `ui/Menus.h`. There is only the dark theme.
Shortcuts: Alt+Enter (also Ctrl+Enter) generate speech, Space play / pause, Q stop (typing in a text field keeps the key), Ctrl+O import, Ctrl+S save MP3,
Ctrl+R recheck, Ctrl+Shift+P check pronunciation, Ctrl+D dictionary, Ctrl+M Model Lab, Ctrl+B side panel, Ctrl+, settings.
Not done: Windows 11 Snap Layouts flyout on the maximize button (needs `WM_NCHITTEST` handling) and Alt+Space system menu.

### Build / run / test (Windows, MinGW Qt 6.11; needs Qt Widgets, Multimedia, Svg, Test)
```bash
cd gui && cmake -S . -B build -G Ninja -DCMAKE_PREFIX_PATH=D:/Apps/Qt6/6.11.1/mingw_64 -DCMAKE_BUILD_TYPE=Release
cmake --build build
build/Voicely.exe
ctest --test-dir build --output-on-failure        # Qt tests (offscreen) + pytest backend tests
```
Developer aid: `TTS_SNAPSHOT=file.png Voicely.exe` renders the window and exits (`TTS_TEST_SNAPSHOT_DIR` does the same inside `tst_qa_gui`).

### Standalone folder (tested)
```bash
python packaging/make_dist.py --build gui/build --qt D:/Apps/Qt6/6.11.1/mingw_64 --verify [--with-google] [--with-asr] [--no-scipy]
```
Creates `dist/Voicely/` (Voicely.exe + Qt via windeployqt, `engine/`, and a private `python/` runtime containing only the dependency closure of
kokoro-onnx, lameenc, numpy, scipy). PyTorch, sounddevice, pytest and models are not included; Kokoro and the ASR model are fetched on request.
`--verify` runs `packaging/verify_dist.py` in a clean environment (no system Python/Qt on PATH): runtime imports, worker protocol 2 actions, real local
synthesis if the Kokoro model exists, MP3 export, and a real launch of the exe. Not done: an installer (Inno Setup/NSIS/WiX) and code signing.

## Model Lab (optional)
A from-scratch, growable Transformer with training, evaluation, expansion and a Qt control panel (Tools > Model Lab, Ctrl+M). It is optional, needs PyTorch, runs in its own worker process, and the speech app never depends on it. See `docs/MODEL_LAB.md` for what it does, the real results and the limitations.
