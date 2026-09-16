#!/usr/bin/env python3
"""LEGACY foreground DesktopDriver benchmark, not the co-working product.

This harness competes for the OS pointer/focus and must never be presented as
validation of independent agent input. Nothing launches without --run. Oracle data stays outside
model observations and can only be written by the fixture's native Save button.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import agent
from server.drivers import DesktopDriver, DriverAbort, _keys, capabilities
from server.perception import _local_ocr_url
from server.schemas import Action, ModelConfig, RunRequest
from benchmarks.provenance import provenance

RUN_PROVENANCE = provenance(ROOT)


class FixtureBoundaryError(DriverAbort):
    """Stop rather than recover from an attempted scope escape."""


class _WindowPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class FixtureInput:
    """DesktopDriver-compatible input sent to exactly one process, never globally."""

    READ_METHODS = {"size", "failSafeCheck", "position", "KEYBOARD_KEYS"}
    KEY_CODES = {"a": 0, "v": 9, "tab": 48, "space": 49, "backspace": 51, "delete": 117, "enter": 36,
                 "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119}
    ALLOWED_CHORDS = {("command", "a"), ("command", "v"), ("tab",), ("shift", "tab"), ("space",),
                      ("backspace",), ("delete",), ("enter",), ("left",), ("right",), ("up",), ("down",), ("home",), ("end",)}

    def __init__(self, real_gui, driver):
        self.real_gui, self.driver = real_gui, driver
        self._event_number = 0
        # PID posting requires a window-local point in addition to screen
        # coordinates. This exported but private symbol is fixture-only; the
        # production PyAutoGUI driver does not rely on it.
        self._coregraphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        try:
            self._set_window_location = self._coregraphics.CGEventSetWindowLocation
        except AttributeError as exc:
            raise FixtureBoundaryError("This macOS version lacks the scoped fixture's window-local event transport.") from exc
        self._set_window_location.argtypes = [ctypes.c_void_p, _WindowPoint]
        self._set_window_location.restype = None

    def __getattr__(self, name):
        if name in self.READ_METHODS:
            return getattr(self.real_gui, name)
        raise FixtureBoundaryError(f"Benchmark input method is not allowed: {name}")

    def screenshot(self):
        """Capture only the owned window; never collect pixels of other apps."""
        import AppKit as A
        import Quartz as Q
        from PIL import Image
        self.driver._verify_fixture()
        capture = Q.CGWindowListCreateImage(Q.CGRectNull, Q.kCGWindowListOptionIncludingWindow,
            self.driver.ready["window_id"], Q.kCGWindowImageBoundsIgnoreFraming | Q.kCGWindowImageNominalResolution)
        if capture is None:
            raise FixtureBoundaryError("Could not capture the dedicated fixture window.")
        representation = A.NSBitmapImageRep.alloc().initWithCGImage_(capture)
        encoded = representation.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
        if encoded is None:
            raise FixtureBoundaryError("Could not encode the dedicated fixture capture.")
        box = self.driver.ready["window_bounds"]
        shot = Image.open(io.BytesIO(bytes(encoded))).convert("RGB")
        logical = (round(box["width"]), round(box["height"]))
        if shot.size != logical:
            shot = shot.resize(logical, Image.Resampling.LANCZOS)
        screen = Image.new("RGB", tuple(self.real_gui.size()), "#e9e7e2")
        screen.paste(shot, (round(box["x"]), round(box["y"])))
        self.driver._verify_fixture()
        return screen

    def _post(self, event):
        import Quartz as Q
        self.driver._verify_fixture()
        self.real_gui.failSafeCheck()
        Q.CGEventPostToPid(self.driver.fixture_pid, event)
        time.sleep(0.025)

    def click(self, x, y, button="left", clicks=1, interval=0.1):
        import AppKit as A
        import Quartz as Q
        if button != "left" or clicks not in (1, 2):
            raise FixtureBoundaryError("Benchmark only allows left clicks within its fixture.")
        self.driver._verify_point(x, y)
        for index in range(clicks):
            self._event_number += 1
            box = self.driver.ready["window_bounds"]
            local = (float(x - box["x"]), float(box["height"] - (y - box["y"])))
            for kind in (A.NSEventTypeLeftMouseDown, A.NSEventTypeLeftMouseUp):
                # PID posting bypasses normal WindowServer hit testing. An
                # NSEvent carries the explicit target window and local point;
                # public CG routing fields preserve that window association.
                native = A.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                    kind, local, 0, time.monotonic(), self.driver.ready["window_id"], None,
                    self._event_number, index + 1, 1.0 if kind == A.NSEventTypeLeftMouseDown else 0.0)
                event = native.CGEvent()
                Q.CGEventSetLocation(event, (float(x), float(y)))
                Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointer, self.driver.ready["window_id"])
                Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, self.driver.ready["window_id"])
                self._set_window_location(event.__c_void_p__(), _WindowPoint(float(x - box["x"]), float(y - box["y"])))
                self._post(event)
            if index + 1 < clicks:
                time.sleep(min(interval, 0.2))

    def doubleClick(self, x, y, interval=0.12, button="left"):
        self.click(x, y, button=button, clicks=2, interval=interval)

    def moveTo(self, x, y, duration=0):
        import Quartz as Q
        self.driver._verify_point(x, y)
        event = Q.CGEventCreateMouseEvent(None, Q.kCGEventMouseMoved, (float(x), float(y)), Q.kCGMouseButtonLeft)
        box = self.driver.ready["window_bounds"]
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointer, self.driver.ready["window_id"])
        Q.CGEventSetIntegerValueField(event, Q.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, self.driver.ready["window_id"])
        self._set_window_location(event.__c_void_p__(), _WindowPoint(float(x - box["x"]), float(y - box["y"])))
        self._post(event)

    def hotkey(self, *keys):
        import Quartz as Q
        keys = tuple(keys)
        if keys not in self.ALLOWED_CHORDS:
            raise FixtureBoundaryError("Benchmark refused a shortcut outside form editing.")
        modifiers = [(55, Q.kCGEventFlagMaskCommand)] if "command" in keys else []
        if "shift" in keys:
            modifiers.append((56, Q.kCGEventFlagMaskShift))
        flags, held = 0, []
        try:
            for code, flag in modifiers:
                flags |= flag
                event = Q.CGEventCreateKeyboardEvent(None, code, True)
                Q.CGEventSetFlags(event, flags)
                self._post(event)
                held.append((code, flag))
            code = self.KEY_CODES[keys[-1]]
            for down in (True, False):
                event = Q.CGEventCreateKeyboardEvent(None, code, down)
                Q.CGEventSetFlags(event, flags)
                self._post(event)
        finally:
            # Modifier release is addressed only to our still-live child, even
            # if its focus changed mid-chord. It never reaches a user's app.
            for code, flag in reversed(held):
                flags &= ~flag
                if self.driver.process.poll() is None and self.driver.process.pid == self.driver.fixture_pid:
                    event = Q.CGEventCreateKeyboardEvent(None, code, False)
                    Q.CGEventSetFlags(event, flags)
                    Q.CGEventPostToPid(self.driver.fixture_pid, event)
                    time.sleep(0.025)


class FixtureProductionInput(FixtureInput):
    """Actual PyAutoGUI input, guarded to the owned, unobscured foreground form."""

    def __init__(self, real_gui, driver):
        # No private CoreGraphics adapter participates in production transport.
        self.real_gui, self.driver = real_gui, driver

    def click(self, x, y, button="left", clicks=1, interval=0.1):
        if button != "left" or clicks not in (1, 2):
            raise FixtureBoundaryError("Benchmark only permits left clicks in its own form.")
        self.driver._verify_point(x, y)
        self.real_gui.click(x, y, button=button, clicks=clicks, interval=interval)
        self.driver._verify_fixture()

    def doubleClick(self, x, y, interval=0.12, button="left"):
        if button != "left":
            raise FixtureBoundaryError("Benchmark only permits left clicks in its own form.")
        self.driver._verify_point(x, y)
        self.real_gui.doubleClick(x, y, interval=interval, button=button)
        self.driver._verify_fixture()

    def moveTo(self, x, y, duration=0):
        self.driver._verify_point(x, y)
        self.real_gui.moveTo(x, y, duration=duration)
        self.driver._verify_fixture()

    def hotkey(self, *keys):
        if tuple(keys) not in self.ALLOWED_CHORDS:
            raise FixtureBoundaryError("Benchmark refused a shortcut outside form editing.")
        self.driver._verify_fixture()
        self.real_gui.hotkey(*keys)
        self.driver._verify_fixture()


class FixtureDesktopDriver(DesktopDriver):
    """Keep real capture/AX/OCR and DesktopDriver actions inside one native form."""

    ALLOWED_ACTIONS = {"click", "double_click", "move", "type", "key", "wait"}

    def __init__(self, process: subprocess.Popen, ready: dict, artifacts: Path, production_input: bool = False):
        super().__init__()
        self.process, self.ready, self.artifacts = process, ready, artifacts
        self.fixture_pid = int(ready["pid"])
        self.capture_count = 0
        self.boundary_rejections = []
        self.visibility_checks = 0
        self.production_input = production_input

    def _verify_system_overlay_transparency(self):
        """Prove Dock's full-screen surface leaves the fixture visually intact.

        These comparison pixels stay transient, are never saved, and never enter
        the model/OCR observation. Observations capture the owned window only.
        """
        import AppKit as A
        import Quartz as Q
        from PIL import Image, ImageChops
        box = self.ready["content_bounds"]
        bounds = Q.CGRectMake(box["x"], box["y"], box["width"], box["height"])
        images = []
        for mode, window_id in ((Q.kCGWindowListOptionIncludingWindow, self.ready["window_id"]),
                                (Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID)):
            raw = Q.CGWindowListCreateImage(bounds, mode, window_id, Q.kCGWindowImageNominalResolution)
            if raw is None:
                raise FixtureBoundaryError("Could not verify native fixture visibility beneath the system overlay.")
            rep = A.NSBitmapImageRep.alloc().initWithCGImage_(raw)
            encoded = rep.representationUsingType_properties_(A.NSBitmapImageFileTypePNG, {})
            images.append(Image.open(io.BytesIO(bytes(encoded))).convert("RGB"))
        if images[0].size != images[1].size:
            raise FixtureBoundaryError("Visible fixture and owned-window capture sizes differ.")
        histogram = ImageChops.difference(images[0], images[1]).convert("L").histogram()
        differing = sum(histogram[25:]) / (images[0].width * images[0].height)
        # Allow only minor rendering/caret timing differences, not occlusion.
        if differing > 0.0015:
            raise FixtureBoundaryError(f"Native fixture is visually obscured by a system overlay ({differing:.3%} changed pixels); no input was sent.")
        self.visibility_checks += 1

    def _verify_fixture(self):
        import Quartz as Q
        from server.accessibility import _NativeAX
        if self.process.poll() is not None or self.process.pid != self.fixture_pid:
            raise FixtureBoundaryError("The benchmark fixture process has ended.")
        # NSWorkspace can cache focus when no Cocoa run loop is serviced. AX
        # system-wide focus is queried synchronously for every input boundary.
        focused_pid = ctypes.c_int()
        success = False
        for attempt in range(3):
            native = _NativeAX()
            focused = system = None
            try:
                system = native.system()
                native.set_timeout(system, 0.4)
                focused = native.attr(system, "AXFocusedApplication")
                success = bool(focused) and native.pid(focused, ctypes.byref(focused_pid)) == 0 and focused_pid.value > 0
            finally:
                if focused:
                    native.release(focused)
                if system:
                    native.release(system)
                native.close()
            if success:
                break
            time.sleep(0.04)
        if not success or focused_pid.value != self.fixture_pid:
            raise FixtureBoundaryError(f"Focus left the benchmark fixture (expected PID {self.fixture_pid}, observed PID {focused_pid.value}); no event was sent to another application.")
        windows = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID) or []
        current = next((w for w in windows if int(w.get(Q.kCGWindowNumber, -1)) == self.ready["window_id"]), None)
        if current is None or int(current.get(Q.kCGWindowOwnerPID, -1)) != self.fixture_pid:
            raise FixtureBoundaryError("The authorized native fixture window is no longer visible.")
        bounds = current.get(Q.kCGWindowBounds, {})
        expected = self.ready["window_bounds"]
        for key, cocoa_key in (("x", "X"), ("y", "Y"), ("width", "Width"), ("height", "Height")):
            if abs(float(bounds.get(cocoa_key, -1)) - float(expected[key])) > 3:
                raise FixtureBoundaryError("The fixture window moved or resized; stale coordinates were refused.")
        content = self.ready["content_bounds"]
        for front_window in windows:
            if int(front_window.get(Q.kCGWindowNumber, -1)) == self.ready["window_id"]:
                break
            if int(front_window.get(Q.kCGWindowOwnerPID, -1)) == self.fixture_pid or float(front_window.get(Q.kCGWindowAlpha, 1)) <= 0:
                continue
            rect = front_window.get(Q.kCGWindowBounds, {})
            x, y, width, height = (float(rect.get(key, 0)) for key in ("X", "Y", "Width", "Height"))
            overlaps = x < content["x"] + content["width"] and x + width > content["x"] and y < content["y"] + content["height"] and y + height > content["y"]
            if overlaps and width > 0 and height > 0:
                owner = str(front_window.get(Q.kCGWindowOwnerName, "unknown"))
                layer = int(front_window.get(Q.kCGWindowLayer, 0))
                if owner == "Dock" and layer == 20 and x <= 0 and y <= 0 and width >= expected["x"] + expected["width"] and height >= expected["y"] + expected["height"]:
                    self._verify_system_overlay_transparency()
                    continue
                raise FixtureBoundaryError(f"Another window occludes the native fixture ({owner}, layer={layer}, bounds={x},{y},{width},{height}); no input was sent.")

    def _verify_point(self, x, y):
        self._verify_fixture()
        box = self.ready["content_bounds"]
        if not (box["x"] <= x < box["x"] + box["width"] and box["y"] <= y < box["y"] + box["height"]):
            raise FixtureBoundaryError("Target is outside the benchmark fixture content area.")

    def _restore_focus(self):
        # Unlike a general desktop run, a benchmark stops when a user switches apps.
        self._verify_fixture()

    def _keyboard_boundary(self):
        self._verify_fixture()
        super()._keyboard_boundary()

    def _hotkey(self, *keys):
        if tuple(keys) not in FixtureInput.ALLOWED_CHORDS:
            raise FixtureBoundaryError("Benchmark refused a shortcut outside form editing.")
        if self.production_input:
            return super()._hotkey(*keys)
        return self._gui.hotkey(*keys)

    async def start(self):
        await super().start()
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        target = NSRunningApplication.runningApplicationWithProcessIdentifier_(self.fixture_pid)
        if target is None or not target.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            raise FixtureBoundaryError("Could not activate the dedicated fixture process.")
        deadline = time.monotonic() + 3
        while True:
            try:
                await asyncio.to_thread(self._verify_fixture)
                break
            except FixtureBoundaryError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(0.05)
        self._foreground_pid = self.fixture_pid
        self._gui = (FixtureProductionInput if self.production_input else FixtureInput)(self._gui, self)

    def _capture(self):
        from PIL import Image
        self._verify_fixture()
        observation = super()._capture()
        self._verify_fixture()
        if self._foreground_pid != self.fixture_pid:
            raise FixtureBoundaryError("Capture focus changed; observation was discarded.")
        box = self.ready["content_bounds"]
        rectangle = (round(box["x"]), round(box["y"]), round(box["x"] + box["width"]), round(box["y"] + box["height"]))
        if rectangle[0] < 0 or rectangle[1] < 0 or rectangle[2] > observation["width"] or rectangle[3] > observation["height"]:
            raise FixtureBoundaryError("Fixture must be fully on the primary screen.")
        original = Image.open(io.BytesIO(base64.b64decode(observation["image"].partition(",")[2]))).convert("RGB")
        masked = Image.new("RGB", original.size, "#e9e7e2")
        masked.paste(original.crop(rectangle), rectangle[:2])
        stream = io.BytesIO()
        masked.save(stream, format="PNG")
        observation["image"] = "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()
        allowed = []
        for element in observation.get("elements", []):
            x, y = element.get("x"), element.get("y")
            if isinstance(x, (int, float)) and isinstance(y, (int, float)) and rectangle[0] <= x < rectangle[2] and rectangle[1] <= y < rectangle[3]:
                allowed.append(element)
        observation["elements"] = allowed
        observation["title"] = self.ready["title"]
        observation["text"] = "Dedicated native AppKit fixture. Only its form content is visible.\n" + "\n".join(
            json.dumps(element, ensure_ascii=False) for element in allowed
        )
        observation["accessibility"] = {"available": bool(allowed), "scope": "fixture_pid"}
        self.capture_count += 1
        frame_dir = self.artifacts / "frames"
        frame_dir.mkdir(exist_ok=True)
        masked.save(frame_dir / f"{self.capture_count:03d}.png")
        return observation

    async def execute(self, action: Action):
        try:
            if action.type not in self.ALLOWED_ACTIONS:
                raise FixtureBoundaryError("Benchmark forbids navigation, launching apps, tabs, drag and other out-of-scope actions.")
            if action.button != "left":
                raise FixtureBoundaryError("Benchmark only permits left mouse input.")
            if action.type == "key" and tuple(_keys(action.key or "", browser=False)) not in FixtureInput.ALLOWED_CHORDS:
                raise FixtureBoundaryError("Benchmark only permits field-editing shortcuts and navigation keys.")
            if action.type == "type" and (len(action.text or "") > 200 or any(c in (action.text or "") for c in "\r\n\t")):
                raise FixtureBoundaryError("Benchmark typing is limited to a single form-field value.")
            self._verify_fixture()
            if action.target or action.x is not None:
                self._verify_point(*self._point(action))
            return await super().execute(action)
        except FixtureBoundaryError as exc:
            self.boundary_rejections.append(str(exc))
            raise


def scripted_driver_smoke(project_name: str):
    """Return a labelled transport self-test planner; no model API is called.

    Every target comes from the current native AX/OCR observation. Fixture source
    geometry and the independent oracle are never used to choose coordinates.
    """
    def target(observation, label, roles, prefer_ocr=False):
        candidates = [element for element in observation.get("elements", [])
            if label.casefold() in str(element.get("text", "")).casefold()]
        native = [element for element in candidates if element.get("source") == "accessibility"
            and str(element.get("role", "")).casefold() in roles]
        ocr = [element for element in candidates if str(element.get("id", "")).startswith("ocr_")]
        if prefer_ocr and len(ocr) == 1:
            return ocr[0]["id"]
        if len(native) == 1:
            return native[0]["id"]
        if len(ocr) == 1:
            return ocr[0]["id"]
        raise RuntimeError(f"Scripted driver self-test could not identify exactly one observed control: {label}")

    async def planner(config, task, observation, history):
        if any(str(item.get("result", "")).startswith("FAILED:") for item in history):
            raise RuntimeError("Scripted driver self-test stopped after an input transport failure.")
        # Freshness checks may request the same step again without executing it.
        stage = sum(str(item.get("action", {}).get("reason", "")).startswith("Scripted driver self-test:")
            and not str(item.get("result", "")).startswith(("FAILED:", "NOT EXECUTED:")) for item in history)
        if stage == 0:
            try:
                control = target(observation, "Project name", {"textfield", "textbox"})
            except RuntimeError:
                # OCR may see the field's value more clearly than its label.
                control = target(observation, "Untitled project", {"textfield", "textbox"})
            action = Action(type="click", target=control, reason="Scripted driver self-test: focus observed project field")
        elif stage == 1:
            action = Action(type="key", key="CMD+A", reason="Scripted driver self-test: select existing field text")
        elif stage == 2:
            action = Action(type="type", text=project_name, reason="Scripted driver self-test: paste the test value")
        elif stage == 3:
            action = Action(type="click", target=target(observation, "Enable notifications", {"checkbox"}, prefer_ocr=True), reason="Scripted driver self-test: click observed checkbox label")
        elif stage == 4:
            action = Action(type="click", target=target(observation, "Save project", {"button"}), reason="Scripted driver self-test: click observed native Save button")
        else:
            visible = str(observation.get("text", "")) + " " + " ".join(str(element.get("text", "")) for element in observation.get("elements", []))
            if "saved successfully" not in visible.casefold():
                raise RuntimeError("Scripted driver self-test did not observe the native saved-successfully message.")
            action = Action(type="done", text="Scripted driver transport self-test completed; this is not an AI model result.")
        return action

    return planner


async def benchmark(args) -> dict:
    import httpx
    base = _local_ocr_url(args.base_url.removesuffix("/v1").removesuffix("/api"))
    smoke = bool(getattr(args, "driver_smoke", False))
    production_input = bool(getattr(args, "production_input", False))
    if not smoke and "cloud" in args.model.lower():
        raise ValueError("Native benchmark accepts only locally installed models.")
    caps = await capabilities()
    if not caps["desktop"]:
        raise RuntimeError(caps["desktop_hint"])
    # Check local installation before opening any UI. Never auto-pull a model.
    if not smoke:
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.post(base + "/api/show", json={"model": args.model})
            response.raise_for_status()
            metadata = response.json()
        if not isinstance(metadata, dict) or metadata.get("remote_host") or metadata.get("remote_model"):
            raise ValueError("The selected model resolves to a remote/cloud service.")
    elif args.ocr_engine != "native":
        raise ValueError("Driver smoke uses native OCR only; no model inference is permitted.")

    nonce = uuid.uuid4().hex
    name = "driver-smoke" if smoke else re.sub(r"[^A-Za-z0-9._-]", "-", args.model)
    artifacts = (args.output or ROOT / "artifacts" / f"native-{name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{nonce[:6]}").resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    ready_path, oracle_path = artifacts / "fixture-ready.json", artifacts / "saved-project.json"
    process = None
    run = None
    driver = None
    original_driver = agent.DesktopDriver
    original_planner = agent.next_action
    started = time.monotonic()
    stop_reason = None
    with (artifacts / "fixture.log").open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen([sys.executable, str(ROOT / "benchmarks" / "native_fixture.py"), "--ready", str(ready_path), "--output", str(oracle_path), "--nonce", nonce], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            async with asyncio.timeout(12):
                while not ready_path.exists():
                    if process.poll() is not None:
                        raise RuntimeError("Native fixture could not start; inspect fixture.log.")
                    await asyncio.sleep(0.05)
            ready = json.loads(ready_path.read_text())
            if ready.get("pid") != process.pid or ready.get("nonce") != nonce:
                raise RuntimeError("Fixture readiness identity did not match the process we launched.")
            driver = FixtureDesktopDriver(process, ready, artifacts, production_input=production_input)
            agent.DesktopDriver = lambda **kwargs: driver
            if smoke:
                agent.next_action = scripted_driver_smoke(args.project_name)
            config = ModelConfig(provider="ollama", base_url=base + "/v1", model="scripted-driver-smoke" if smoke else args.model, vision=False,
                                 perception="ocr", ocr_engine=args.ocr_engine, ocr_model=args.ocr_model, ocr_base_url=base, max_tokens=args.max_tokens)
            request = RunRequest(target="desktop", approval_mode="auto", max_steps=args.max_steps,
                desktop_pid=process.pid, desktop_window_id=ready["window_id"],
                task=f'In the already open "{ready["title"]}" native window, replace Project name with exactly "{args.project_name}", enable the "Enable notifications" checkbox, then CLICK "Save project". Confirm the visible saved-successfully message. Stay within this window. Use only click, type, field-editing key shortcuts (CMD+A, TAB, SPACE, arrows, BACKSPACE), wait, and done. Do not open or navigate to any application, file, website or menu. For existing text, click its field then CMD+A before typing. Save must be a mouse click on the button.')
            run = agent.Run(request, config, "http://127.0.0.1/unused-benchmark-demo")
            run.task = asyncio.create_task(run.execute_loop())
            deadline = time.monotonic() + args.timeout
            while not run.task.done():
                if run.status in {"awaiting_input", "awaiting_approval"}:
                    stop_reason = "model_requested_human_intervention"
                    await run.stop()
                    break
                if time.monotonic() >= deadline:
                    stop_reason = "benchmark_timeout"
                    await run.stop()
                    break
                await asyncio.sleep(0.1)
            await run.task
            oracle = json.loads(oracle_path.read_text()) if oracle_path.exists() else None
            checks = {"saved_by_fixture_pid": bool(oracle and oracle.get("writer_pid") == process.pid),
                      "fresh_nonce": bool(oracle and oracle.get("nonce") == nonce),
                      "native_save_click": bool(oracle and oracle.get("source") == "native_save_button_mouse_up"),
                      "correct_project_name": bool(oracle and oracle.get("project_name") == args.project_name),
                      "notifications_enabled": bool(oracle and oracle.get("notifications_enabled") is True)}
            session = run.snapshot()
            if smoke:
                session["model"] = None
                session["provider"] = "scripted_driver_smoke"
            report = {"benchmark": "legacy_native_appkit_desktop", "co_working_product_validation": False,
                      "planner_mode": "scripted_driver_smoke" if smoke else "real_model", "model": None if smoke else args.model, "vision": False,
                      "ocr_engine": args.ocr_engine, "elapsed_seconds": round(time.monotonic() - started, 3),
                      "run_status": run.status, "stop_reason": stop_reason, "steps": run.step,
                      "oracle_checks": checks, "oracle_pass": all(checks.values()), "model_claimed_complete": not smoke and run.status == "completed",
                      "false_completion": not smoke and run.status == "completed" and not all(checks.values()),
                      "scripted_sequence_complete": smoke and run.status == "completed",
                      "passed": all(checks.values()) and run.status == "completed", "boundary_rejections": driver.boundary_rejections,
                      "input_transport": ("Legacy foreground DesktopDriver: PyAutoGUI pointer + public Quartz keyboard + native clipboard, guarded to the owned fixture" if production_input else "Legacy fixture-only CGEventPostToPid + NSEvent window routing + private CGEventSetWindowLocation; clipboard and foreground still shared"),
                      "production_desktop_input_validated": False,
                      "legacy_pyautogui_transport_validated": production_input and all(checks.values()) and run.status == "completed",
                      "screen_capture_mode": "owned_window_id_only", "verified_system_overlay_checks": driver.visibility_checks,
                      "fixture_pid": process.pid, "artifact_directory": str(artifacts), "oracle": oracle,
                      "session": session}
            if smoke:
                report["model_calls"] = 0
            report["provenance"] = RUN_PROVENANCE
            (artifacts / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return report
        finally:
            if run and run.task and not run.task.done():
                await run.stop()
            agent.DesktopDriver = original_driver
            agent.next_action = original_planner
            if driver:
                await driver.close()
            # Only terminate the child we launched; never kill a user application.
            if process and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)


def main():
    parser = argparse.ArgumentParser(description="Benchmark a local model or run a labelled driver smoke on an owned AppKit form with OCR + AX.")
    parser.add_argument("--run", action="store_true", help="Explicitly launch/control the dedicated native fixture")
    parser.add_argument("--driver-smoke", action="store_true", help="Run a clearly labelled scripted native driver self-test; no model is called")
    parser.add_argument("--production-input", action="store_true", help="Use production PyAutoGUI pointer + public Quartz keyboard, guarded to the dedicated foreground fixture")
    parser.add_argument("--model", default="qwen3-vl:2b")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ocr-engine", choices=("native", "glm_ocr"), default="native")
    parser.add_argument("--ocr-model", default="glm-ocr:latest")
    parser.add_argument("--project-name", default="ComputerUSE Native Verified")
    parser.add_argument("--max-steps", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, help="New output directory; must not already exist")
    args = parser.parse_args()
    if not args.run:
        parser.error("Nothing was launched. Supply --run to explicitly open and operate the benchmark's own native window.")
    if sys.platform != "darwin":
        parser.error("This native benchmark requires macOS.")
    if not 1 <= args.max_steps <= 50 or not 30 <= args.timeout <= 900 or not 256 <= args.max_tokens <= 16384:
        parser.error("Use 1–50 steps, a 30–900 second timeout, and 256–16384 output tokens.")
    if not args.project_name.strip() or len(args.project_name) > 100 or any(c in args.project_name for c in "\r\n\t"):
        parser.error("Project name must be one nonempty line of at most 100 characters.")
    try:
        report = asyncio.run(benchmark(args))
    except (Exception, KeyboardInterrupt) as exc:
        print(f"Native benchmark stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps({key: report[key] for key in ("planner_mode", "model", "passed", "oracle_pass", "run_status", "steps", "elapsed_seconds", "artifact_directory")}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
