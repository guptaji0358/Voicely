# Pronunciation quality control

## What it does
1. **Before synthesis** (`analyze_text`): finds numbers, dates, units, abbreviations, acronyms, heteronyms, names, technical identifiers
   and non-English text, using heuristics (no word list is bundled). Findings point into the ORIGINAL text. The text is never rewritten;
   only user-approved dictionary entries change what is spoken, and every application is recorded.
2. **Generation** keeps the audio as per-chunk files in a *session* (see `docs/PROTOCOL.md`).
3. **Recheck Speech** (`recheck`) runs per chunk:
   * ASR transcript (no document text is given to the recogniser as a prompt, so it cannot be biased toward the expected words),
   * comparison after removing harmless differences (case, punctuation, quotes, contractions, number formats: `2024` = `twenty twenty four`,
     `$5.50` = `five dollars and fifty cents`, `1st` = `first`; English only - other languages are compared as plain folded words),
   * missing / inserted / substituted words and repetitions, each with the span in the source text and the time range in the audio,
   * phoneme-level comparison with espeak-ng for every disagreeing word (homophones are informational, near-misses are reported as
     "suspected pronunciation discrepancy"),
   * recogniser word confidence (stricter for names, numbers and technical terms),
   * acoustic sanity checks on the audio itself (silence, clipping, implausible speech rate, long gaps).
4. **Review / repair**: replay the affected chunk, add a respelling or IPA, regenerate only that chunk, recheck it. The repaired mix is a
   *candidate*; the original stays byte-for-byte until you accept the repair.

## Statuses
`not_checked`, `verified`, `needs_review`, `failed`. They are rule-based summaries, **not** a calibrated quality score.
`verified` means every automated check that exists passed. It is never produced by a transcript match alone, and words a transcript
cannot confirm (heteronyms such as "read") always produce `needs_review`.

## Honest limitations
* A recogniser can transcribe a mispronounced word with the expected spelling, and can make errors of its own.
* No forced alignment / phoneme-recognition model is bundled; adding one (e.g. a CTC phoneme model) would be the next step for stronger
  acoustic verification. The `ASRProvider` interface and the per-chunk structure are ready for it.
* Pronunciation controls depend on the backend: **local (Kokoro)**: respelling + IPA (checked against Kokoro's phoneme vocabulary);
  **google (Gemini TTS)**: respelling/style only - IPA entries are stored but reported as unsupported, not faked.
* Auto-repair uses only approved sources (dictionary entries, user-supplied candidates, validated LLM suggestions if enabled). If the
  voice returns the same result after a change, the repair stops and says so.
* The ASR model is **not bundled**: install `faster-whisper` and download a model from Settings > Pronunciation (consent-gated). Without
  it, Recheck Speech is disabled with an explanation; generation and export are unaffected.
* Real-model behaviour is covered only by `tests/test_real_optional.py` (skipped unless the model is installed). All other ASR tests use
  a scripted MOCK recogniser and are labelled as such in the report (`[MOCKED]`).

## Dictionary
Entries: written form, language, respelling (`hint`), IPA, approved replacement (`correction`), category, case sensitivity, optional
context words (`only after` / `only before`). Matching is whole-word and Unicode-aware (`Ann` never matches inside `Annual`), longest phrase
first, no overlaps. Stored in `%LOCALAPPDATA%\speech_engine\pronunciation\dictionary.json`, versioned (`version` + `history/` snapshots).
Import/export JSON or CSV (all-or-nothing validation). Every change is checked against the regression set first (see `docs/LEARNING.md`).
