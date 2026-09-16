"""Driver tests never send events to the real desktop."""

import asyncio
import base64
import io
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PIL import Image

from server.drivers import BrowserDriver, DesktopDriver, DriverAbort, _keys, validate_navigation_url


def action(kind, **kwargs):
    return SimpleNamespace(**({"type": kind, "x": None, "y": None, "target": None,
        "text": None, "key": None, "url": None, "direction": None, "amount": None,
        "seconds": None, "button": "left", "duration": 0.2, "end_x": None,
        "end_y": None, "app": None} | kwargs))


@pytest.mark.parametrize("url", ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,hello", "ftp://example.com", "https://user:secret@example.com", "https://example.com\n", "http://", "https://example.com:bad"])
def test_reject_unsafe_navigation(url):
    with pytest.raises(ValueError):
        validate_navigation_url(url)


@pytest.mark.parametrize("url", ["about:blank", "https://example.com/path?q=1", "http://127.0.0.1:8765/demo"])
def test_allow_normal_navigation(url):
    assert validate_navigation_url(url) == url


def test_key_aliases_and_rejection():
    assert _keys("CTRL+L", browser=True) == ["Control", "l"]
    assert _keys("COMMAND+SHIFT+ArrowRight", browser=True) == ["Meta", "Shift", "ArrowRight"]
    assert _keys("ENTER", browser=False) == ["enter"]
    with pytest.raises(ValueError):
        _keys("Control+run arbitrary code", browser=True)


def test_perception_targets_bounds_and_unknown_ids():
    driver = DesktopDriver()
    driver.set_observation({"width": 1280, "height": 800, "elements": [
        {"id": "ocr_1", "x": 120.5, "y": 79.2, "width": 80, "height": 20}
    ]})
    assert driver._point(action("click", target="ocr_1")) == (120, 79)
    for point in ({"x": -1, "y": 10}, {"x": 1280, "y": 0}, {"x": float("nan"), "y": 0}, {"target": "body button"}):
        with pytest.raises(ValueError):
            driver._point(action("click", **point))


def test_retina_capture_uses_logical_mouse_coordinates(monkeypatch):
    driver = DesktopDriver()
    driver._gui = MagicMock()
    driver._gui.size.return_value = (640, 400)
    driver._gui.screenshot.return_value = Image.new("RGB", (1280, 800), "white")
    monkeypatch.setattr(driver, "_desktop_context", lambda *_: {})
    observation = driver._capture()
    png = base64.b64decode(observation["image"].split(",", 1)[1])
    assert (observation["width"], observation["height"]) == (640, 400)
    assert Image.open(io.BytesIO(png)).size == (640, 400)


def test_unicode_paste_restores_previous_text_even_on_error(monkeypatch):
    driver = DesktopDriver()
    driver._gui = MagicMock()
    content = {"text": "previous text"}
    driver._clipboard = SimpleNamespace(
        paste=lambda: content["text"], copy=lambda text: content.update(text=text)
    )
    monkeypatch.setattr("server.drivers.time.sleep", lambda _: None)
    driver._paste("你好，世界 👋")
    assert content["text"] == "previous text"
    driver._gui.hotkey.side_effect = RuntimeError("paste failed")
    with pytest.raises(RuntimeError, match="paste failed"):
        driver._paste("更多中文")
    assert content["text"] == "previous text"


def test_clipboard_does_not_overwrite_concurrent_user_copy(monkeypatch):
    driver = DesktopDriver()
    driver._gui = MagicMock()
    content = {"text": "before"}
    driver._clipboard = SimpleNamespace(
        paste=lambda: content["text"], copy=lambda text: content.update(text=text)
    )
    driver._gui.hotkey.side_effect = lambda *args: content.update(text="user copy")
    monkeypatch.setattr("server.drivers.time.sleep", lambda _: None)
    driver._paste("model text")
    assert content["text"] == "user copy"


def test_macos_chord_flags_and_release_on_stop(monkeypatch):
    driver = DesktopDriver()
    driver._gui = MagicMock()
    events = []
    driver._quartz = SimpleNamespace(kCGEventFlagMaskCommand=1, kCGEventFlagMaskShift=2,
        kCGEventFlagMaskControl=4, kCGEventFlagMaskAlternate=8, kCGHIDEventTap=0,
        CGEventCreateKeyboardEvent=lambda source, code, down: {"code": code, "down": down},
        CGEventSetFlags=lambda event, flags: event.update(flags=flags),
        CGEventPost=lambda tap, event: events.append(dict(event)))
    driver._mac_keycodes = {"command": 55, "shift": 56, "a": 0, "v": 9}
    monkeypatch.setattr("server.drivers.time.sleep", lambda _: None)
    driver._hotkey("command", "a")
    assert events == [{"code": 55, "down": True, "flags": 1},
                      {"code": 0, "down": True, "flags": 1},
                      {"code": 0, "down": False, "flags": 1},
                      {"code": 55, "down": False, "flags": 0}]
    driver._gui.hotkey.assert_not_called()
    events.clear()
    checks = iter([None, DriverAbort("stop")])
    def boundary():
        failure = next(checks)
        if failure:
            raise failure
    monkeypatch.setattr(driver, "_keyboard_boundary", boundary)
    with pytest.raises(DriverAbort, match="stop"):
        driver._hotkey("command", "v")
    assert events == [{"code": 55, "down": True, "flags": 1}, {"code": 55, "down": False, "flags": 0}]

    # A failed release is fatal, and remaining held modifiers still get released.
    events.clear()
    monkeypatch.setattr(driver, "_keyboard_boundary", lambda: None)
    def post(tap, event):
        events.append(dict(event))
        if event["code"] == 0 and not event["down"]:
            raise OSError("keyboard transport disconnected")
    driver._quartz.CGEventPost = post
    with pytest.raises(DriverAbort) as failure:
        driver._hotkey("command", "a")
    assert failure.value.fatal is True
    assert events[-1] == {"code": 55, "down": False, "flags": 0}


def test_mock_desktop_uses_ocr_target_and_rejects_browser_only_actions():
    async def run():
        driver = DesktopDriver()
        driver._gui = MagicMock()
        driver._restore_focus = lambda: None
        driver.set_observation({"width": 1280, "height": 800, "elements": [
            {"id": "obj_1", "x": 400, "y": 200}
        ]})
        await driver.execute(action("click", target="obj_1"))
        driver._gui.click.assert_called_once_with(400, 200, button="left")
        with pytest.raises(ValueError, match="桌面不支援"):
            await driver.execute(action("select_option", target="obj_1", text="value"))
        await driver.close()
    asyncio.run(run())


def test_desktop_drag_release_and_terminal_failsafe():
    async def run():
        driver = DesktopDriver()
        driver._gui = MagicMock()
        driver._restore_focus = lambda: None
        driver._gui.position.return_value = (300, 200)
        driver.set_observation({"width": 1000, "height": 800, "elements": []})
        await driver.execute(action("click", x=100, y=100, button="right"))
        driver._gui.click.assert_called_once_with(100, 100, button="right")
        await driver.execute(action("move", x=200, y=100))
        await driver.execute(action("drag", x=100, y=100, end_x=300, end_y=200))
        driver._gui.dragTo.assert_called_once_with(300, 200, duration=0.2, button="left", mouseDownUp=False)
        driver._gui.platformModule._mouseUp.assert_called_once_with(300, 200, "left")
        assert not driver._pressed_buttons

        class FailSafeException(Exception):
            pass

        driver._gui.dragTo.side_effect = FailSafeException("corner")
        with pytest.raises(DriverAbort) as error:
            await driver.execute(action("drag", x=100, y=100, end_x=300, end_y=200))
        assert error.value.fatal is True
        assert not driver._pressed_buttons
        assert driver._gui.platformModule._mouseUp.call_count == 2
        await driver.close()
    asyncio.run(run())


def test_app_launch_passes_one_argument_without_shell(monkeypatch):
    driver = DesktopDriver()
    run = MagicMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr("server.drivers.platform.system", lambda: "Darwin")
    monkeypatch.setattr("server.drivers.subprocess.run", run)
    driver._open_app("Visual Studio Code")
    assert run.call_args.args[0] == ["/usr/bin/open", "-a", "Visual Studio Code"]
    assert "shell" not in run.call_args.kwargs
    with pytest.raises(ValueError):
        driver._open_app("--args echo nope")
    driver._open_url("https://example.com/path?q=value")
    assert run.call_args.args[0] == ["/usr/bin/open", "https://example.com/path?q=value"]
    with pytest.raises(ValueError):
        driver._open_url("javascript:alert(1)")


def test_restore_observed_app_focus_before_typing(monkeypatch):
    driver = DesktopDriver()
    driver._foreground_pid = 42
    active = MagicMock()
    active.processIdentifier.side_effect = [99, 42]
    workspace = MagicMock()
    workspace.sharedWorkspace.return_value.frontmostApplication.return_value = active
    target = MagicMock()
    target.isTerminated.return_value = False
    apps = MagicMock()
    apps.runningApplicationWithProcessIdentifier_.return_value = target
    monkeypatch.setattr("server.drivers.platform.system", lambda: "Darwin")
    monkeypatch.setattr("server.drivers.time.sleep", lambda _: None)
    fresh_pids = iter([99, 42])
    monkeypatch.setattr("server.accessibility.focused_application_pid", lambda **kwargs: next(fresh_pids))
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSWorkspace=workspace, NSRunningApplication=apps, NSApplicationActivateIgnoringOtherApps=2))
    driver._restore_focus()
    apps.runningApplicationWithProcessIdentifier_.assert_called_once_with(42)
    target.activateWithOptions_.assert_called_once_with(2)


def test_desktop_preserves_fresh_ax_identity_and_refuses_unknown_focus(monkeypatch):
    driver = DesktopDriver()
    cached_app = MagicMock()
    cached_app.processIdentifier.return_value = 99
    cached_app.localizedName.return_value = "Old app"
    workspace = MagicMock()
    workspace.sharedWorkspace.return_value.frontmostApplication.return_value = cached_app
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(NSWorkspace=workspace,
        NSApplicationActivateIgnoringOtherApps=2, NSRunningApplication=MagicMock()))
    monkeypatch.setattr("server.drivers.platform.system", lambda: "Darwin")
    monkeypatch.setattr("server.accessibility.read_accessibility", lambda *args: {
        "pid": 42, "title": "Fresh observed app", "elements": []})
    context = driver._desktop_context(1000, 800)
    assert context["pid"] == 42 and context["title"] == "Fresh observed app"
    driver._foreground_pid = 42
    monkeypatch.setattr("server.accessibility.focused_application_pid", lambda **kwargs: None)
    with pytest.raises(RuntimeError, match="取消"):
        driver._restore_focus()


def test_close_waits_for_cancelled_desktop_operation():
    async def run():
        driver = DesktopDriver()
        began, release, finished = threading.Event(), threading.Event(), threading.Event()

        def operation():
            began.set()
            release.wait(timeout=2)
            finished.set()

        pending = asyncio.create_task(driver._run_sync(operation))
        await asyncio.to_thread(began.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        closing = asyncio.create_task(driver.close())
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing
        assert finished.is_set()
    asyncio.run(run())


def test_isolated_browser_dom_actions_and_external_perception_targets():
    pytest.importorskip("playwright.async_api")

    async def run():
        driver = BrowserDriver()
        try:
            await driver.start()
        except RuntimeError as exc:
            if "Chromium" in str(exc):
                pytest.skip(str(exc))
            raise
        try:
            await driver._page.set_content('''<!doctype html><html><head><title>Driver test</title></head><body>
              <label for="name">姓名</label><input id="name">
              <button onclick="document.querySelector('#result').textContent=document.querySelector('#name').value">送出</button>
              <p id="result">尚未送出</p>
              <select aria-label="顏色"><option value="red">紅</option><option value="blue">藍</option></select>
              <button style="display:none">隱藏</button>
              <a href="javascript:alert(1)">Unsafe</a>
            </body></html>''')
            observation = await driver.observe()
            assert observation["width"] == 1280 and observation["height"] == 800
            assert all(element["text"] != "隱藏" for element in observation["elements"])
            text_field = next(e for e in observation["elements"] if e["text"] == "姓名")
            submit = next(e for e in observation["elements"] if e["text"] == "送出")
            await driver.execute(action("type", target=text_field["id"], text="繁體中文測試"))
            await driver.execute(action("click", target=submit["id"]))
            assert await driver._page.locator("#result").inner_text() == "繁體中文測試"
            unsafe = next(e for e in observation["elements"] if e["text"] == "Unsafe")
            with pytest.raises(ValueError):
                await driver.execute(action("click", target=unsafe["id"]))
            with pytest.raises(ValueError):
                await driver.execute(action("click", target="button:first-child"))
            observation["elements"].append({"id": "ocr_1", "x": submit["x"], "y": submit["y"]})
            driver.set_observation(observation)
            await driver.execute(action("click", target="ocr_1"))
            select = next(e for e in observation["elements"] if e["text"] == "顏色")
            assert select["options"][1]["label"] == "藍"
            await driver.execute(action("select_option", target=select["id"], text="藍"))
            assert await driver._page.locator("select").input_value() == "blue"
            new_observation = await driver.observe()
            assert next(e for e in new_observation["elements"] if e["text"] == "姓名")["id"] == text_field["id"]
            assert next(e for e in new_observation["elements"] if e["text"] == "送出")["id"] == submit["id"]
            await driver.execute(action("click", target=submit["id"]))
            await driver._page.locator("button").first.evaluate("el => el.replaceWith(el.cloneNode(true))")
            replaced = await driver.observe()
            assert next(e for e in replaced["elements"] if e["text"] == "送出")["id"] != submit["id"]
            with pytest.raises(ValueError):
                await driver.execute(action("click", target=submit["id"]))
            await driver.execute(action("new_tab"))
            tabs = (await driver.observe())["tabs"]
            assert len(tabs) == 2 and tabs[1]["active"]
            await driver.execute(action("switch_tab", text=tabs[0]["id"]))
            assert await driver._page.title() == "Driver test"
            await driver.execute(action("close_tab", text=tabs[1]["id"]))
            assert len((await driver.observe())["tabs"]) == 1
            previous_ids = {e["id"] for e in (await driver.observe())["elements"]}
            await driver._page.reload()
            await driver._page.set_content('<button>New document</button>')
            after_navigation = await driver.observe()
            assert not previous_ids & {e["id"] for e in after_navigation["elements"]}
        finally:
            await driver.close()
    asyncio.run(run())


def test_accessibility_excludes_password_values_and_clips_boxes(monkeypatch):
    from server.accessibility import read_accessibility

    class FakeAX:
        trusted = lambda self: True
        focused = lambda self: 1
        release = lambda self, node: None
        retain = lambda self, node: node
        hash = lambda self, node: node
        pid = lambda self, node, out: -1
        attr = lambda self, node, name: 2 if name == "AXFocusedWindow" else None
        children = lambda self, node: [3, 4, 5, 6] if node == 2 else []

        def attributes(self, node):
            return {
                1: {"AXTitle": "Example"},
                2: {"AXRole": "AXWindow", "AXTitle": "Window", "AXPosition": (0, 0), "AXSize": (1000, 800)},
                3: {"AXRole": "AXTextField", "AXSubrole": "AXSecureTextField", "AXTitle": "密碼", "AXValue": "SECRET", "AXPosition": (20, 20), "AXSize": (100, 30)},
                4: {"AXRole": "AXButton", "AXTitle": "確定", "AXPosition": (950, 700), "AXSize": (200, 200)},
                5: {"AXRole": "AXCheckBox", "AXTitle": "通知", "AXValue": 1.0, "AXPosition": (20, 60), "AXSize": (100, 30)},
                6: {"AXRole": "AXTextField", "AXTitle": "名稱", "AXValue": "中文", "AXPosition": (20, 100), "AXSize": (100, 30)},
            }[node]

    result = read_accessibility(1000, 800, backend=FakeAX())
    assert result["available"] is True
    assert "SECRET" not in str(result)
    assert next(e for e in result["elements"] if e["role"] == "CheckBox")["checked"] is True
    assert next(e for e in result["elements"] if e.get("value") == "中文")["role"] == "TextField"
    button = next(e for e in result["elements"] if e["role"] == "Button")
    assert (button["x"], button["y"], button["width"], button["height"]) == (975, 750, 50, 100)
