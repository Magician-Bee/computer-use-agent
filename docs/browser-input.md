# Isolated browser input

`BrowserDriver` owns a separate Chromium context. Its `BrowserPointer` sends a minimum-jerk trajectory through real Playwright `mouse.move` calls and tracks each page's last acknowledged position. Default motion runs at scheduled 60 Hz with distance-based duration from 0.25 to 1.2 seconds. It uses no OS pointer or global keyboard API. `physical_input_untouched=true` is reported only for headless operation; headed browser startup is not covered by that claim.

A purple SVG cursor is rendered in a closed shadow root attached outside body text. Python updates the overlay only after a corresponding Playwright mouse command completes. The host is aria-hidden and ignores pointer events; it has no text or actionable DOM elements. Observation metadata exposes the actual cursor position, sample count, pressed buttons and `perception_exclusions=[{kind:"executor_cursor",bbox:[x,y,22,30]}]`. Perception excludes detections fully inside that region and marks partial occlusions as unknown.

DOM clicks validate the current node and each iframe embedding at the target point. The pointer follows its trajectory, settles, and validates again before mouse-down. If a hover handler moves or covers the target, the action refuses without pressing. Double click emits two balanced down/up pairs with correct click counts. Drag uses the same continuous trajectory while its page mouse button is down. Pause interrupts the trajectory before another sample/click; cancellation and close drain mouse-up before destroying the browser context. An interruption after a mouse-down can still complete a click on release, so the agent must reobserve instead of assuming the action had no effect.

Semantic DOM input remains available for text fields, select controls and file inputs. Those operate the isolated browser's own state. They do not claim to be physical keystroke simulation.

The pinned macOS headless shell's ordinary `Meta+C/X/V` source path uses a memory-only `ClipboardNonBacked`. Each driver launches its own browser process; different contexts in one shared process would not by themselves prove separate clipboards. The [versioned source audit](browser-clipboard-audit.md) records the exact binary/version and official source chain. It also identifies a special OS Find pasteboard path whose reachability through the current Action API remains unproved. This is static evidence for ordinary copy/cut/paste, not a dynamic or universal clipboard-isolation certification.

## Evidence

`tests/test_browser_motion.py`: **10 passing tests**, including actual headless pointer events, screenshot cursor pixels, moving-target rejection, pause without click, drag pause/cancel/close release, per-tab positions, native dblclick event and iframe input. Pure trajectory math checks bounded endpoints and acceleration/deceleration. These tests do not operate the physical desktop.

`artifacts/browser-pointer-proof/report.json` is explicitly labeled `scripted_driver_smoke`, `model=null`, `model_calls=0`. It contains 66 actual browser pointer events and an independent page saved-state oracle. `screenshot.png` shows the real executor cursor after the successful click. This is driver transport evidence, not a model benchmark.

## Actual live-frame evidence

`scripts/diagnose_browser_preview.py` ran the production `BrowserPreview.stream` concurrently with an actual 1.8-second Playwright trajectory and click in headless Chromium. The action including settling/click took 1.994 seconds. It received **13 JPEG frames, 11 distinct JPEG hashes and 10 distinct cursor positions measured directly from image pixels**. There were 109 actual pointer-move samples and 112 total pointer events. The final native browser click changed the independent fixture's saved-state oracle to true, with one balanced down/up pair.

The proof uses the real `Run` observation object. Its identity and complete SHA256 remained unchanged throughout preview streaming, as did the driver's selected page. Streaming did not call model inference, reobserve the page or select another tab. Artifacts are in `artifacts/browser-live-preview-proof/`: `report.json`, `start.jpg`, `middle.jpg`, `end.jpg`. The middle frame was visually inspected and shows the purple cursor in transit before the Save button changes. The report explicitly states `planner_mode=scripted_driver_smoke`, `model=null`, `model_calls=0` and `ui_frontend_validated=false`.

Reproduce using a new output directory:

```sh
.venv/bin/python scripts/diagnose_browser_preview.py --output artifacts/browser-live-preview-proof-new
```

## Real HTTP and browser image decoding

`scripts/diagnose_preview_http.py` adds a separate HTTP proof. It started its own uvicorn process on ephemeral loopback port 55142 with `COMPUTERUSE_PORT=55142`, leaving the user's port 8765 untouched. It configured the fixed demo provider through the real API, created a `browser_visible=false` session, and attached a second headless Chromium monitor before approving the demo's type and click actions.

The monitor's ordinary `<img src="/api/sessions/{id}/view">` received HTTP **200**, `multipart/x-mixed-replace; boundary=computeruse-frame`, and decoded **1280×800** images. A parallel HTTP reader collected **14 JPEG parts**. Pixel evidence from both the received JPEGs and the monitor's decoded canvas showed the purple cursor moving from roughly `(3,7)` through `(518,264)` to `(817,412)`. The middle decoded frame was visually inspected. No page errors, console errors or image decoding errors occurred.

The task reached `completed`. A test-only subclass reads the actual demo DOM immediately before the production driver closes, providing an oracle independent of the task's completion claim: input `ComputerUSE`, visible result `測試任務已完成：歡迎，ComputerUSE。`. The launcher traps model calls; the count remained **0**. Production source hashes stayed unchanged throughout the proof. The monitor, agent browser and owned server were closed afterward. This test-only close hook and monitor route live in the diagnosis script; no production runtime or frontend files were changed.

Evidence: `artifacts/browser-http-preview-proof/report.json`, `monitor-start.jpg`, `monitor-middle.jpg`, `monitor-end.jpg`, `monitor-final.png`, and sampled raw HTTP JPEG frames. The report is labeled `scripted_driver_smoke`, `model=null`, `model_calls=0`, `production_frontend_ui_validated=false`.

```sh
.venv/bin/python scripts/diagnose_preview_http.py --output artifacts/browser-http-preview-proof-new
```

## Production workspace UI over real HTTP

The production frontend now has a separate real HTTP E2E report（local-only evidence; not included: `artifacts/workspace-live-preview-proof-run2/report.json`）, produced by [diagnose_workspace_preview.py](../scripts/diagnose_workspace_preview.py). All **27 checks passed** using the real `frontend/dist`, API, demo Run, driver and preview on an owned temporary loopback server. Both browsers were headless; port 8765 was untouched. The server and browsers exited after the run.

The UI started two fixed demo sessions with `browser_visible=false`. Each ordinary image element decoded `/view` at **1280 × 800 before any action approval**. Two UI approvals completed the first demo; an independent close-time DOM read confirmed the exact name and visible success result. The second session was stopped through the UI before any action. Both terminal paths switched to the last `/screenshot`. There were **zero model calls, console errors/warnings and page errors**; runtime and production-build hashes stayed unchanged.

The active desktop screenshot（local-only evidence; not included: `artifacts/workspace-live-preview-proof-run2/workspace-active-desktop.png`） and completed 390 px mobile screenshot（local-only evidence; not included: `artifacts/workspace-live-preview-proof-run2/workspace-completed-mobile.png`） were visually reviewed. The mobile document width was exactly 390 px. The first diagnostic attempt（local-only evidence; not included: `artifacts/workspace-live-preview-proof/report.json`） remains retained with `passed=false` because of a Python Playwright argument error in the harness; the success is a separate run, not an overwritten result.

```sh
.venv/bin/python scripts/diagnose_workspace_preview.py --output artifacts/workspace-live-preview-proof-new
```

Choose a new output directory; evidence is never overwritten. This proof is explicitly scripted/no-AI and covers production UI integration for the fixed local demo. It does not validate model reasoning, real-site login, native application co-working, or a release Gate. Shared native desktop control remains disabled pending a separate isolated-worker design.
