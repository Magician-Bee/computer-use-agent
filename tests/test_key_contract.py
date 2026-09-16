import pytest
from jsonschema import Draft202012Validator

from server.key_contract import canonical_key_chord, key_schema, keyboard_catalog
from server.drivers import _keys
from server.providers import ProviderError, next_action
from server.schemas import ModelConfig


@pytest.mark.parametrize("chord,expected", [
    ("control+a", "CTRL+A"), ("command+shift+s", "SHIFT+CMD+S"),
    ("ArrowLeft", "LEFT"), ("RETURN", "ENTER"), ("alt+tab", "ALT+TAB"),
    ("CTRL+PLUS", "CTRL+PLUS"), ("ctrl+shift", "CTRL+SHIFT"), ("/", "/"),
])
def test_supported_browser_chords_canonicalize_and_match_driver_parser(chord, expected):
    obs = {"target": "browser"}
    actual = canonical_key_chord(chord, obs)
    assert actual == expected
    Draft202012Validator(key_schema(obs)).validate(actual)
    assert _keys(actual, browser=True)


@pytest.mark.parametrize("chord", ["select_option", "click", "hello", "CTRL++", "CTRL+CTRL+A", "A+B", "A+CTRL", "F13", "F24", "F25"])
def test_non_keys_and_invalid_chords_are_rejected(chord):
    with pytest.raises(ValueError):
        canonical_key_chord(chord, {"target": "browser"})


def test_platform_and_negotiated_backend_key_limits():
    mac = {"target": "desktop", "platform": "Darwin"}
    assert canonical_key_chord("F20", mac) == "F20"
    for key in ("F21", "F24", "INSERT"):
        with pytest.raises(ValueError):
            canonical_key_chord(key, mac)
    windows = {"target": "desktop", "platform": "Windows"}
    assert canonical_key_chord("F24", windows) == "F24"
    assert canonical_key_chord("INSERT", windows) == "INSERT"
    limited = {"target": "desktop", "keyboard_capabilities": {"keys": ["TAB", "LEFT"], "modifiers": []}}
    assert canonical_key_chord("TAB", limited) == "TAB"
    with pytest.raises(ValueError):
        canonical_key_chord("CTRL+A", limited)


def test_background_candidate_cannot_request_shortcuts_without_capability():
    obs = {"target": "desktop", "input_transport": "cua_background_ax_candidate"}
    assert canonical_key_chord("LEFT", obs) == "LEFT"
    for key in ("CMD+A", "ENTER", "ALT+TAB"):
        with pytest.raises(ValueError):
            canonical_key_chord(key, obs)


async def test_fake_action_name_as_key_is_rejected_before_driver_even_without_sampler(monkeypatch):
    async def complete(*args, **kwargs):
        return '{"reason":"Select an option","type":"key","key":"select_option"}'
    monkeypatch.setattr("server.providers.complete", complete)
    with pytest.raises(ProviderError, match="key"):
        await next_action(ModelConfig(provider="custom", base_url="http://localhost/v1"), "task", {"target": "browser"}, [])


async def test_every_advertised_browser_key_executes_in_real_headless_transport():
    """A parser accepting a name alone is insufficient (F13..24 regressed this)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<input autofocus><script>window.events=[];document.addEventListener("keydown",e=>{'
                'window.events.push({key:e.key,ctrl:e.ctrlKey,shift:e.shiftKey});e.preventDefault()})</script>')
            keys, _ = keyboard_catalog({"target": "browser"})
            for key in sorted(keys) + ["CTRL+A", "CTRL+PLUS", "CTRL+SHIFT"]:
                chord = canonical_key_chord(key, {"target": "browser"})
                await page.keyboard.press("+".join(_keys(chord, browser=True)))
            actual = await page.evaluate("window.events")
            assert len(actual) >= len(keys) + 3
            assert any(item["key"] == "+" and item["ctrl"] for item in actual)
            assert any(item["key"] == "Shift" and item["ctrl"] for item in actual)
        finally:
            await browser.close()
