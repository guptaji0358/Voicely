# GUI <-> worker protocol, version 1

Transport: the Qt app starts `python -X utf8 -m speech_engine.worker` with `QProcess` (program + argument list, no shell).
Framing: **one JSON object per line** (UTF-8, `\n` terminated, ASCII-escaped by the worker so a line never contains a raw
newline). Requests on **stdin**, protocol messages on **stdout** (nothing else is ever written there; the worker
re-points fd 1 to stderr and owns a private duplicate). **stderr** = logs/tracebacks (shown in the GUI's Diagnostics panel).
Audio never travels over the pipe - only file paths and metadata. The GUI never parses human text to decide success.

Every message carries `"protocol_version": 1`. A request carries `request_id` (string, chosen by the client) and `action`.

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
