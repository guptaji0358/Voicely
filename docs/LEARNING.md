# LLM assistance and learning from corrections

## What "learning" means here (and what it does not)
| Stage | Mechanism | Status |
|---|---|---|
| 1. Versioned pronunciation dictionary built from approved corrections | persistent JSON + snapshots (`speech_engine/pron_dictionary.py`) | **implemented** |
| 2. Regression set from user-approved examples | `feedback.build_cases` (only corrections whose recheck succeeded) | **implemented** |
| 3. Evaluate changes against that set before they go live | `feedback.gate_change`; the GUI asks before overriding a failing change | **implemented** (text-level, offline) |
| 4. Improve preprocessing / prompts / rules from examples | approved corrections can be sent as few-shot examples in LLM prompts (opt-in, disclosed); rule changes are made by a developer and gated by stage 3 | **partly**: prompt examples only; no automatic rule mining |
| 5. Fine-tuning a model | design below | **not implemented** |

A correction is "learned" only when it has been stored in the dictionary (stage 1). The LLM itself does not change. No foundation model
is trained or fine-tuned on user requests, ever, automatically.

## Feedback records
Saved only after you approve a correction **and** have switched on *Save corrections I approve on this computer* (Settings >
Pronunciation). Audio samples need a second switch. Each record: the sentence, the word, language, voice and backend, pronunciation
settings, the verification result, the approved correction, whether the recheck confirmed it, optional audio, and versions
(engine, dictionary, recogniser, LLM model, prompt). Everything lives in `%LOCALAPPDATA%\speech_engine\feedback\`, can be viewed, exported
to a folder you choose, or deleted (one record or all). Nothing is uploaded. Withdrawing consent also withdraws the audio consent.

## LLM layer (`speech_engine/llm/`)
* Optional and **off by default**; speech generation, recheck and export never call it.
* Provider interface (`LLMProvider`); implemented: Google Gemini via `google-genai` (key from `GEMINI_API_KEY`, never stored, never in
  config files) and a scripted mock for tests. No silent fallback to another provider.
* Network use is explicit: each request needs `allow_network`, and the GUI asks for confirmation (provider, model, how much text).
* Timeouts, bounded retries with back-off (rate-limit / 5xx / timeout only), client-side rate limit, structured error codes.
* The reply is DATA. It must be a JSON object; every suggestion is validated: the word must occur in your text (no hallucinated words),
  respellings are plain letters, IPA must be pronounceable by the voice, number expansions must keep their value, abbreviation expansions must
  start like the abbreviation, lengths are capped. Rejected items are reported. Nothing is executed and nothing is applied until you add it
  to the dictionary yourself (`source = llm-approved`).
* The LLM never verifies audio (it is not given any) and the explanation task says so.
* Not verified here: the default model name `gemini-2.5-flash`, quotas and pricing - check Google's current model list and pricing, and set
  the model in Settings. The Google provider has been tested with a fake client only.

## Future fine-tuning workflow (design only)
Only worthwhile with (a) a model and licence that permit fine-tuning, (b) enough properly licensed, consented examples (thousands of
verified corrections), and (c) compute. It must be a separate, reproducible job - never part of the app:
1. export approved feedback (user-consented) to a versioned dataset with a data card and licence/consent manifest;
2. split train / validation / **held-out test** by sentence and by term, and freeze the test set;
3. train with pinned code, seeds and base-model hash; log the config;
4. evaluate against the held-out set and the current regression set (term accuracy via ASR round trip + human listening on a sample);
5. ship only if it beats the baseline without regressions; keep the previous model for rollback; record model + dataset versions in feedback
   records and reports.
