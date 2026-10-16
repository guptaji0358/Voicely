# TTS Commands Cheat Sheet

Run every command from the `TTS` folder:

```bash
cd "C:\Users\lenovo\OneDrive\Desktop\projects\TTS"
```

General shape: `python -m speech_engine.cli <command> [options]`

---

## 1. First-time setup

```bash
pip install -r requirements.txt
python -m speech_engine.cli download-model --yes          # ~310 MB model + voices (one time)
python -m speech_engine.cli download-model --yes --int8   # smaller ~80 MB, slightly lower quality
```

Without `--yes` it only shows what would be downloaded.

## 2. Check that everything works

```bash
python -m speech_engine.cli test                    # check local + google
python -m speech_engine.cli test --backend local    # check only local
python -m speech_engine.cli test --synth            # also speak a short test phrase
```

## 3. See the voices

```bash
python -m speech_engine.cli voices
python -m speech_engine.cli voices --backend local   # also list raw Kokoro voices
```

Built-in voice profiles: `original`, `boy`, `deep`, `reference`.

## 4. Speak (play through speakers)

```bash
python -m speech_engine.cli speak --text "Hello world" --voice original
python -m speech_engine.cli speak --text "Hi, I'm a kid!" --voice boy
python -m speech_engine.cli speak --input document.txt --voice original
type document.txt | python -m speech_engine.cli speak       # from piped text
```

## 5. Save to a file instead of playing

```bash
python -m speech_engine.cli speak --input document.txt --voice original --output speech.wav
python -m speech_engine.cli speak --text "Hello" -o hello.wav --sample-rate 16000
python -m speech_engine.cli speak --text "Hello" -o hello.pcm --format pcm
```

Formats: `wav`, `mp3` or `pcm` (raw 16-bit mono), chosen from the file extension.

### Save as MP3

```bash
python -m speech_engine.cli speak --text "Hello world" --voice original --output speech.mp3
python -m speech_engine.cli speak --input document.txt --voice original --format mp3 --bitrate 192k --output narration.mp3
python -m speech_engine.cli speak --input document.txt -o narration.mp3 --bitrate 128k --force
```

- Needs `pip install lameenc` (no FFmpeg needed). `test` shows `[mp3 export] OK`.
- Format is taken from the extension; `--format` must match it.
- `--bitrate` (MP3 only): 128k, 160k, 192k (default), 256k, 320k ... Default sample rate 44.1 kHz.
- `--force` overwrites an existing file; without it you are asked (or it fails in scripts).
- The file appears only if the whole export succeeded.

## 6. Voice options

| Option | Meaning |
|---|---|
| `--voice NAME` | Voice profile (default `original`) |
| `--speed 0.9` | Speaking rate, 0.5 to 2.0 |
| `--style "warm, slow"` | Delivery instruction (Google backend only) |
| `--backend local` / `google` | Which engine (default `local`, offline) |
| `--profiles-file my.json` | Your own or overridden profiles |
| `--json` | Machine-readable output on stdout |
| `-q` | No progress output |
| `--format wav\|mp3\|pcm` | Output format (inferred from `--output`) |
| `--bitrate 192k` | MP3 bitrate |
| `--force` | Overwrite existing output |

## 7. Google backend (optional, sends text to Google)

PowerShell:

```powershell
$env:GEMINI_API_KEY = "your-key"
python -m speech_engine.cli speak --backend google --text "Hello" --voice original
```

The key is read only from the environment. Never put it in a file or command.

## 8. Compare voices / evaluate

```bash
python -m speech_engine.cli evaluate --voices original,boy,deep --output-dir out --rating-sheet rate.csv
python -m speech_engine.cli evaluate --voices original --items conversational,question
python -m speech_engine.cli evaluate --voices original --multilingual
```

Listen to the WAV files in `out`, then fill in `rate.csv`.

## 9. Custom voice profile

Create `my.json`:

```json
{
  "original": { "speed": 0.95 },
  "narrator": {
    "local_blend": { "af_heart": 0.6, "bf_emma": 0.4 },
    "pitch_semitones": 0,
    "speed": 0.95
  }
}
```

Use it:

```bash
python -m speech_engine.cli speak --profiles-file my.json --voice narrator --text "Testing"
```

## 10. Use from the OCR app (Image text Etracter)

Inside the chat:

```
:speak                  read the last response (local, voice original)
:speak boy              local, voice boy
:speak google original  Google backend
:stop                   stop speaking
:mp3                    Save as MP3 (opens a save dialog)
:mp3 voice=boy C:\out.mp3   save with a chosen voice and path
```

## 11. Python API

```python
from speech_engine import SpeechEngine, make_backend, get_profile

eng = SpeechEngine(make_backend("local"), get_profile("boy"))
eng.speak("Hello")                       # blocking
h = eng.speak_async("Long text...")      # background; a new call interrupts it
h.cancel()
eng.synthesize_to_file("Hello", "out.wav")
eng.close()
```

## 12. Tests

```bash
python -m pytest tests -q
```

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 2 | Bad input (empty text, missing or non-UTF-8 file) |
| 3 | Configuration problem |
| 4 | Backend unavailable (missing package, model or API key) |
| 5 | Synthesis failed |
| 6 | Rate limit |
| 7 | Audio output problem |
| 8 | MP3 encoder (`lameenc`) missing |
| 130 | Interrupted (Ctrl+C) |

## Tips

- Use quotes around text containing spaces or special characters.
- Press Ctrl+C to stop speaking.
- If a command fails, run `python -m speech_engine.cli test` to see what is missing.
