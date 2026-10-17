# GUI <-> worker protocol, versions 1 and 2

Transport: the Qt app starts `python -X utf8 -m speech_engine.worker` with `QProcess` (program + argument list, no shell).
Framing: **one JSON object per line** (UTF-8, `\n` terminated, ASCII-escaped by the worker so a line never contains a raw
newline). Requests on **stdin**, protocol messages on **stdout** (nothing else is ever written there; the worker
re-points fd 1 to stderr and owns a private duplicate). **stderr** = logs/tracebacks (shown in the GUI's Diagnostics panel).
Audio never travels over the pipe - only file paths and metadata. The GUI never parses human text to decide success.

Every message carries `"protocol_version"`: **2** is current; the worker still answers **1** requests exactly as before (and replies with `1`). Protocol-2-only actions sent with version 1 get `UNSUPPORTED_PROTOCOL`. A request carries `request_id` (string, chosen by the client) and `action`.

## Messages from the worker
| Kind | Shape |
|---|---|
| ready | `{"event":"ready","request_id":null,"pid":123}` sent once at startup |
| progress | `{"request_id":"..","event":"progress","progress":0-100,"message":".."}` |
| success | `{"request_id":"..","status":"success", ...action fields}` |
| error | `{"request_id":"..","status":"error","code":"..","message":".."}` |
| cancelled | `{"request_id":"..","status":"cancelled"}` |

Error codes: `MALFORMED_REQUEST`, `UNSUPPORTED_PROTOCOL`, `UNKNOWN_ACTION`, `INVALID_INPUT`, `CONFIGURATION`,
`BACKEND_UNAVAILABLE`, `RATE_LIMIT`, `ENCODER_UNAVAILABLE`, `SYNTHESIS_FAILED`, `BUSY`, `NOT_RUNNING`, `INTERNAL_ERROR`.
Client-generated codes (process level, never sent by the worker): `WORKER_START_FAILED`, `WORKER_EXITED`,
`WORKER_NOT_RUNNING`, `TIMEOUT`. When the process dies every pending request is completed with `WORKER_EXITED`.

## Actions
* `health` `{backend?}` -> `{python, executable, ready, backends:{name:{available,network,details,problems}}, mp3:{available,encoder,problem}}`
* `capabilities` -> `{voices:[{name,description,speed,kind}], backends:{name:{languages,speed,style,network}}, formats, bitrates}`
* `synthesize` `{text, backend, voice, language?, speed?, style?, output_path, output_format: wav|mp3, bitrate_kbps?, overwrite?}`
  -> progress events, then `{output_path, format, bytes, duration_s, sample_rate, backend, voice, chunks, [bitrate_kbps, mp3_frames]}`
* `export` `{input_path (16-bit mono WAV), output_path, output_format: wav|mp3, bitrate_kbps?, overwrite?}` - re-encodes an
  already generated WAV without re-synthesizing.
* `cancel` `{target: <request_id>}` -> `{cancelling:true}`; the target job then ends with `status:"cancelled"`, temp file removed.
* `shutdown`. Closing stdin has the same effect (running job is cancelled, worker exits 0).

One job at a time: a second `synthesize`/`export` while one runs gets `BUSY`. Files are written to a hidden `.name.PID.part`
file, validated (MP3: every frame header walked) and renamed only on success; an existing destination is refused unless
`overwrite` is true (the GUI sets it only after the native save dialog's overwrite confirmation).

## Test-only backend
`SPEECH_ENGINE_ALLOW_MOCK=1` registers backend `mock` (a sine tone, **not speech**); text containing `[[fail]]` forces an error,
`SPEECH_ENGINE_MOCK_DELAY` slows each chunk. Without the variable `mock` does not exist, so it can never be used silently.


## Protocol 2 additions (pronunciation QA, dictionary, feedback, LLM, ASR)
Audio never travels over the pipe: a *session folder* chosen by the GUI holds `manifest.json`, per-chunk WAV files
(`chunks/0003.g0.wav`; `g1`, `g2`... are regenerations) and `assembled/{original,accepted,candidate}.wav`. `original.wav` and every
`g0` file are never modified. Only paths and metadata cross the protocol; the worker validates every path, type and range.

New error codes: `CONSENT_REQUIRED`, `LLM_DISABLED`, `LLM_NOT_CONFIGURED`, `LLM_NETWORK_NOT_ALLOWED`, `LLM_AUTH`, `LLM_RATE_LIMIT`,
`LLM_TIMEOUT`, `LLM_INVALID_OUTPUT`, `LLM_ERROR`. Validation problems are `INVALID_INPUT`; unavailable recogniser: `BACKEND_UNAVAILABLE`.
Jobs (one at a time, cancellable, with progress): `synthesize`, `export`, `recheck`, `regenerate`, `llm_analyze`, `llm_explain`, `asr_download`.
Everything else answers immediately.

| Action | Request (besides `request_id`) | Success fields |
|---|---|---|
| `health` | `{}` | v1 fields + `asr{provider,available,model,problems}`, `llm{...}`, `dictionary{version,entries}`, `feedback{consent,...}` |
| `capabilities` | `{}` | v1 fields + `pronunciation{backend:{respelling,ipa}}`, `protocol_versions` |
| `analyze_text` | `text, backend, language` | `findings[{id,kind,start,end,text,severity,reason,suggestions,occurrences}]` (offsets into the ORIGINAL text), `counts`, `applications`, `unsupported`, `controls`, `notes` |
| `synthesize` | v1 fields + `session_dir`, `dictionary?` | v1 fields + `session{id,dir,manifest,dictionary_version,dictionary_applications,original_path}`; WAV only |
| `recheck` (job) | `session_dir, which: accepted\|candidate\|original, chunks?, thresholds?` | `summary{status: verified\|needs_review\|failed, issue_count, issues[], chunks[], chunk_status, mean_word_confidence, asr{...,mocked}, limitations, elapsed_s}` |
| `regenerate` (job) | `session_dir, mode: review\|auto, chunks?, candidates[{chunk,written,hint,ipa}], max_attempts, use_llm, allow_network, thresholds?` | `outcomes[{chunk,status: repaired\|improved\|unresolved, attempts[], limitations[], suggested_entry}]`, `candidate_path`, `original_path`, `summary`, `original_preserved:true` |
| `accept_repair` / `discard_repair` | `session_dir` | `accepted_path`, `summary` / `discarded` |
| `segment` | `session_dir, chunk, version?, which?` | `path, duration_s, text, spoken, method` (for replay) |
| `session_info` | `session_dir` | chunks, selection, paths, last summary |
| `dictionary` | `op: list\|search\|add\|update\|delete\|import\|export, ...` | `entries/version`, or `added/updated/deleted` + `regression` when the change would break approved corrections |
| `feedback` | `op: status\|set_consent\|record\|list\|delete\|delete_all\|export\|regression` | see `docs/LEARNING.md`; `record` needs consent (`CONSENT_REQUIRED`) |
| `llm_status` / `llm_configure` | `settings{...}` | provider status (offline check, no secrets) |
| `llm_analyze` (job) | `text, backend, language, allow_network` | validated `suggestions[]`, `rejected[]`, `meta` |
| `llm_explain` (job) | `intended, recognized, context, allow_network` | `explanation` (text only; the model did not hear audio) |
| `asr_status` / `asr_download` (job) | `confirm:true` for download | provider status / local model path |

Stale results: the GUI keeps the id of the job it is waiting for and ignores any other final message (and ignores analysis answers for
text that has since changed). A worker crash completes every pending request with `WORKER_EXITED`.
