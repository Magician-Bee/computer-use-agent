from types import SimpleNamespace

import pytest

from server import background


async def test_global_input_cannot_satisfy_background_capability(monkeypatch):
    async def capabilities():
        return {"available": True, "physical_input_untouched": False, "backend": "physical-only"}
    monkeypatch.setattr(background.importlib, "import_module", lambda _: SimpleNamespace(capabilities=capabilities))
    assert (await background.desktop_status())["available"] is False


async def test_missing_backend_stays_disabled_without_legacy_fallback(monkeypatch):
    def missing(_):
        raise ImportError("not installed")
    monkeypatch.setattr(background.importlib, "import_module", missing)
    assert (await background.desktop_status())["available"] is False
    with pytest.raises(RuntimeError, match="背景鍵盤"):
        background.create_desktop_driver()


async def test_background_without_executor_cursor_is_not_ready(monkeypatch):
    async def capabilities():
        return {"available": True, "physical_input_untouched": True, "agent_cursor_available": False}
    monkeypatch.setattr(background.importlib, "import_module", lambda _: SimpleNamespace(capabilities=capabilities))
    assert (await background.desktop_status())["available"] is False


async def test_explicit_inventory_is_independent_from_input_enablement(monkeypatch):
    expected = [{"pid": 41, "window_id": 73, "title": "Background fixture"}]
    calls = []
    async def capabilities():
        return {"available": False, "installed": True, "physical_input_untouched": False}
    async def list_windows():
        calls.append("list")
        return expected
    module = SimpleNamespace(capabilities=capabilities, list_windows=list_windows)
    monkeypatch.setattr(background.importlib, "import_module", lambda _: module)
    await background.desktop_status()
    assert calls == [], "health must not launch a native backend"
    assert await background.desktop_windows() == {"windows": expected, "available": False}
    assert calls == ["list"]


def test_selected_window_is_forwarded_without_foreground_inference(monkeypatch):
    calls = []
    def driver(**kwargs):
        calls.append(kwargs)
        return object()
    monkeypatch.setattr(background.importlib, "import_module", lambda _: SimpleNamespace(CuaDriver=driver))
    background.create_desktop_driver(pid=41, window_id=73)
    assert calls == [{"pid": 41, "window_id": 73}]


@pytest.mark.parametrize("identity", [
    {"desktop_pid": 41}, {"desktop_window_id": 73},
    {"desktop_pid": True, "desktop_window_id": 73},
    {"desktop_pid": 41, "desktop_window_id": 0},
])
def test_invalid_window_identity_rejected(identity):
    from pydantic import ValidationError
    from server.schemas import RunRequest
    with pytest.raises(ValidationError):
        RunRequest(task="Background fixture", target="desktop", **identity)
