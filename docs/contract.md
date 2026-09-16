# ComputerUSE implementation contract

The authoritative validation contract is `server/schemas.py`; routes are in `server/app.py`. This document describes the implemented behavior.

## Runtime

Python 3.11+, FastAPI, Pydantic v2, httpx; React/TypeScript/Vite. The service binds to loopback. One run owns the computer/browser at a time, including cleanup. Keys stay in process memory and are omitted from snapshots and exports. History is memory-only (40 runs).

`RunRequest`: task, `target=browser|desktop`, `approval_mode=always|auto`, up to 200 actions, HTTP(S) `start_url`, `browser_visible` (default false), and up to 20 explicit absolute upload file paths. Desktop execution requires paired positive integer `desktop_pid` and `desktop_window_id`; foreground autodetection is forbidden. A visible Chromium window is an explicit manual-handoff option. Its context is discarded on cleanup.

States: `starting`, `running`, `awaiting_approval`, `paused`, `awaiting_input`, `completed`, `stopped`, `failed`, `limit_reached`. Pause and steering invalidate in-flight decisions. Stop cancels inference, releases approval/input waits, and awaits cleanup. Finite native input must finish releasing keys/clipboard before another run starts.

Each turn observes, plans one action, refreshes/remaps its target, executes, then records visible effects from the next observation. Input delivery does not certify task success. Repeated identical actions, provider failures, or stale grounding stop the loop. Model completion is checked on a fresh observation; this is still model verification. Benchmarks use independent state oracles.

## Observation and actions

Observations include screenshot dimensions, title, URL, visible text, target elements, tabs, apps, allowed uploads, downloads and perception metadata. Coordinates are screenshot pixels, top-left origin; element boxes use center coordinates. Driver text is retained internally for grounding. Text-only serialization excludes screenshots and hidden fields.

`Action` supports navigation, click/double-click/move/drag, text input, keys, scroll, wait, open app, browser history, HTML option selection, tabs, upload, ask user and done. Required arguments and finite bounds are validated. Arbitrary selectors, JavaScript, shell commands and code execution are not action types.

Browser DOM targets use observed handles. OCR/YOLO targets use observed coordinates. IDs must exist in the current observation. DOM input replaces text. Production desktop input is disabled pending independent-input certification; the legacy global clipboard/pointer transport is not a product fallback.

## Providers and perception

`next_action(config, task, observation, history)` returns one validated action. Adapters implement OpenAI Chat Completions, Anthropic Messages, Gemini generateContent, native Ollama and custom OpenAI-compatible services. `vision=false` gates images in serialization and request construction. See `ollama-compatibility.md` for compatibility adaptations.

`enrich_observation` appends native OCR, optional local GLM-OCR transcript, and optional local YOLO-World boxes. Auto perception uses browser DOM and desktop OCR/Accessibility. Explicit OCR enriches either target. GLM-OCR is recognition, not the planner. Apple Vision/RapidOCR provide real text boxes; ungrounded GLM text receives no fabricated coordinates. Optional YOLO weights are not silently downloaded. See `perception.md`.

## Drivers

`BrowserDriver(start_url='about:blank', headless=True)` and candidate `CuaDriver(pid, window_id)` expose async `start`, `observe`, `prepare_for_action`, `execute(Action)`, `close`; sync `set_observation`. Interruptible drivers expose sync `interrupt_action` and async `resume_actions` that drains old input before resuming. Browser `configure_files` binds the upload allowlist and download directory before startup.

Uploads pin file identity and read verified bytes, maximum 64 MiB/file. Downloads go to a per-run directory and use opaque IDs. Downloaded files are not executed. Browser contexts are independent of existing user sessions. `background.desktop_status` requires both `physical_input_untouched=true` and `agent_cursor_available=true` before enabling desktop input. The candidate currently returns false; health checks never launch native processes.

The legacy native benchmark includes both a fixture-specific PID transport and a guarded global input experiment. Neither demonstrates the user's independent-input requirement. These foreground experiments are not run while the user works on the shared desktop.

HermesBridge is a separately tested integration candidate, not yet the production decision engine. Its pinned source export and per-process HERMES_HOME exclude the user's configuration, credentials and session database. Only four parent-owned gateway tools are exposed. IPC conformance uses a scripted local model server. See `hermes-integration.md` for remaining integration work.

## HTTP API

Mutations require `Content-Type: application/json` and `X-ComputerUse: 1`. Foreign browser origins are rejected. API responses are uncached and validation errors omit input values.

- `GET /api/health`, `GET /api/models`
- `GET /api/desktop/windows` (explicit read-only discovery; never auto-selects the foreground window)
- `GET/POST /api/config`, `POST /api/config/test`, `DELETE /api/config/key`
- `GET/POST /api/sessions`, `GET /api/sessions/{id}`
- `POST /api/sessions/{id}/approve`, `/stop`, `/pause`, `/resume`, `/input`
- `GET /api/sessions/{id}/screenshot`, `/export`, `/files/{file_id}`
- `GET /api/sessions/{id}/view`: read-only multipart JPEG live frames from the browser's already-selected page. Preview never changes model observations or owns input/browser lifetime. Desktop live streaming is not enabled.

Config responses omit `api_key` and include `has_key`. Empty submitted keys reuse existing keys only for the same provider and effective endpoint. Config is not persisted. Demo executes a labeled fixed browser self-check without AI and cannot control the desktop.
