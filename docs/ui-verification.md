# Frontend verification

Updated on 2026-09-11 (Asia/Taipei). Earlier real-server checks used `http://127.0.0.1:8765`. The reproducible mock suite and the production-workspace E2E on an owned temporary HTTP server below are separate evidence layers.

## Build

```sh
cd frontend
npm install
npm run build
```

The final TypeScript and Vite production build passed. The generated application is in `frontend/dist/`.

## Flows checked

| Check | Result |
| --- | --- |
| Load the real workspace | `/api/health`, `/api/config`, and `/api/sessions` returned HTTP 200; the service and DOM/OCR capability indicators populated from the backend. |
| Desktop layout | Visually reviewed at 1440 × 1000. Composer, task history, preview, and settings entry points rendered without clipping. |
| Mobile layout | Visually reviewed at 390 × 844. Both viewport width and document scroll width were **390 px**, with no horizontal overflow. |
| Open/close model settings | Settings drawer opened; Escape closed it. The drawer scrolls independently and keeps save/test controls visible. |
| Discover installed Ollama models | `/api/models` returned `minicpm-v4.6:latest`, `qwen3-vl:2b`, and `glm-ocr:latest`. The first two appear as decision-model presets; GLM-OCR is identified separately as a perception model. |
| Select a decision model | Clicking the `qwen3-vl:2b` preset populated the model ID. No inference request was sent. |
| Select GLM-OCR perception | Selecting the GLM-OCR engine revealed model `glm-ocr:latest` and endpoint `http://127.0.0.1:11434`. The UI explains that OCR receives the image separately while a text-only decision model receives text. |
| Save settings | Saving the demo configuration through the headless UI returned **POST `/api/config` → 200** and closed the drawer. The request uses an explicit field allowlist and excludes response-only `has_key`. |
| Demo execution | The integrating agent verified the real UI demo with two action approvals and a completed session. This validates the fixed local-browser example, not arbitrary model task success. |
| Browser console | The final-build headless save flow reported **0 errors and 0 warnings**. |

The real-server checks above did not invoke the connection-test button or supply cloud model credentials. The reproducible suite below exercises that button against a mock response only.

## Reproducible headless suite

The checks now live in source files under `frontend/tests/`, outside the ignored output folders:

- `ui-contract.test.mjs` — Node's built-in test runner and Playwright browser interactions.
- `mock_api.py` — Python standard-library API fixture, using an ephemeral loopback port.
- `README.md` — prerequisites, commands, coverage, and test limits.

```sh
cd frontend
npm ci
npm run test:install-browser
npm test
```

Requirements: Node.js 20+, Python 3.9+ (`python3`), and Chromium for the pinned **Playwright 1.62.0**. `npm test` builds the production frontend first. Playwright uses its standard shared browser cache; this verification reused the Chromium already installed for Python Playwright. There are no Codex-specific paths or plugin dependencies. `COMPUTERUSE_TEST_PYTHON` can select another Python executable.

The final harness completed successfully: **16 tests passed, 0 failed** — one parent test containing **15 workflow groups**, in approximately 16 seconds excluding the build. It verified:

1. Model, endpoint, and in-memory key drafts survive provider clicks and switches.
2. Connection-test errors scroll into view, and config saves exclude `has_key` while sending the required control header.
3. Explicit saved-key deletion works, without writing browser local/session storage.
4. Opening demo settings does not silently replace an existing provider configuration.
5. Browser visibility defaults to false; explicit opt-in, approval mode and upload allowlists reach the API. Active browser images use `/view`, decode the synthetic multipart image, and identify it as live monitoring.
6. Pause/resume, steering while paused, and visible-browser human handoff remain usable; observation timestamp updates do not reconnect the stream.
7. A pending approval cannot be submitted twice before the backend consumes it.
8. Completion switches the image to `/screenshot`; summaries, exported JSON, downloaded content and missing-file errors work.
9. New Task resets auto mode, upload authorization and physical-browser visibility; desktop mode hides browser-only uploads.
10. A terminal `session.error` appears even without an error event.
11. A missing-session 404 refreshes the model configuration, releases the active task, and stops stale polling.
12. Desktop window discovery and selection make only read requests; unavailable independent input blocks both the button and keyboard submission.
13. A ready mock backend requires an explicit PID/window selection and receives only that identity, with browser visibility and uploads disabled.
14. Window disappearance and discovery errors clear stale selections and keep tasks disabled; the desktop target panel also fits a 390 px viewport.
15. A background browser run sends `browser_visible=false`, retries a failed preview request after recovery, and changes to the last screenshot after Stop.

The browser stayed headless. The fixture made no model, OCR, native-desktop, or real-backend calls. Browser requests outside the fixture origin were intercepted; optional font resources received empty responses. No JavaScript exceptions occurred. The missing-file and expired-session scenarios intentionally returned HTTP 404; the preview interruption intentionally returned HTTP 503.

## Visual artifacts

These generated QA artifacts are intentionally ignored by Git:

- `frontend/output/playwright/workspace-desktop.png` — desktop workspace.
- `frontend/output/playwright/workspace-mobile.png` — complete mobile workspace.
- `frontend/output/playwright/settings-ollama.png` — Ollama configuration and installed-model presets.

## Scope of verification

The automated suite verifies frontend behavior and request contracts against controlled API responses. Its upload check verifies the allowlist sent by the UI, not a real file upload. Actual website login, provider API compatibility, OCR accuracy, native desktop behavior, and general task reliability require the separate backend tests and model benchmarks. A passing mock UI suite does not establish success for arbitrary real-world tasks.

## Background browser preview and optional handoff

The advanced browser setting **顯示獨立瀏覽器視窗** now defaults to **off**, including New Task and retry-from-task resets. Browser runs use the background executor unless the user explicitly opts into a physical window for manual handoff. The helper says that a physical window may take focus; this choice does not certify native desktop input independence. An earlier UI check covered the previous default-on behavior; that behavior has been superseded by this change.

Active browser tasks load `GET /api/sessions/{id}/view` with a stable URL, including while paused or awaiting input. The UI labels this as live monitoring, separate from the fixed decision observation in the text-view tab. Completed, failed and stopped tasks load the final `/screenshot` instead. Active image failures show a reconnect message and retry after three seconds. A deliberately selected visible-browser session still explains how to complete login or verification and reply to continue.

The final mock suite uses a tiny synthetic multipart image to verify the route, browser decoding, no reconnection during state updates, error recovery, and switching to a final screenshot. It does **not** establish actual cursor movement or real-site login. Separate backend evidence in `artifacts/browser-live-preview-proof/report.json` contains 13 real JPEG frames, 11 distinct frames and a successful fixture click through BrowserPreview; that report explicitly marks `ui_frontend_validated=false` and `model_calls=0`. Neither evidence layer is a general desktop co-working certification.

## Background desktop target diagnostics

The desktop composer now reads `GET /api/desktop/windows` and refreshes `/api/health`, lists distinguishable PID/window IDs without activating a window, and reports backend, independent input, and Agent cursor readiness. An unavailable backend stays disabled even if windows can be listed. Starting a desktop task requires an explicit selection and sends `desktop_pid`/`desktop_window_id`; new tasks clear the selection. Browser-window visibility is explicitly described as separate from background desktop input.

This addition was tested only against the headless mock API. No real window listing, Cua call, model inference, or native input was executed by this UI verification. Build and all 15 workflow groups passed. The mock error view was visually inspected; at 390 px the document width remained 390 px. Additional ignored screenshots: `frontend/output/playwright/desktop-target-desktop.png` and `frontend/output/playwright/desktop-target-mobile.png`. These show simulated responses, not a live certified desktop backend.

## Production workspace over real HTTP

[The successful report](../artifacts/workspace-live-preview-proof-run2/report.json) was generated by [diagnose_workspace_preview.py](../scripts/diagnose_workspace_preview.py). It runs the actual `frontend/dist`, FastAPI routes, demo Run, BrowserDriver and BrowserPreview under a newly allocated loopback port. Both workspace and executor browsers are headless. It reuses the existing preview HTTP diagnostic's test-only model-call trap and read-only demo DOM oracle; task creation, both action approvals and Stop happen through production UI buttons.

All 27 checks passed, with zero model calls, zero console errors/warnings and zero page errors. Both runs sent `browser_visible=false`, decoded the real multipart `/view` image at 1280 × 800 before any approval, then switched to `/screenshot` after completion or Stop. The first run completed its two fixed actions and the independent close oracle read the exact name and success text. The second run stopped before executing any action. Production runtime/build hashes were unchanged, and the owned server exited. Port 8765 was not contacted or modified.

The real UI screenshots were visually inspected: [active desktop](../artifacts/workspace-live-preview-proof-run2/workspace-active-desktop.png) at 1280 px and [completed mobile](../artifacts/workspace-live-preview-proof-run2/workspace-completed-mobile.png) at 390 px. The mobile document width was exactly 390 px.

The [first attempt](../artifacts/workspace-live-preview-proof/report.json) remains preserved with `passed=false`: the diagnostic itself used an incorrect positional argument to Python Playwright's `wait_for_function`, before frame verification. The second attempt fixed this harness call. Do not treat the first attempt's frontend-used metadata as a passing E2E result.

To repeat after building, choose a **new** output folder (the script refuses to overwrite previous evidence):

```sh
npm --prefix frontend run build
.venv/bin/python scripts/diagnose_workspace_preview.py --output artifacts/workspace-live-preview-proof-rerun
```

This closes the production UI → real HTTP → background executor preview integration check for the fixed local demo. It is scripted with no AI and does not establish general model reliability, native desktop input independence, real-site login or any release Gate.
