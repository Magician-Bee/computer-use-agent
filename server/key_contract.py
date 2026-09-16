"""Pure keyboard argument contract shared by model/tool adapters.

No keyboard is read or operated here. A driver may narrow the default platform
catalog with an observed keyboard_capabilities manifest; policy still decides
whether an otherwise valid shortcut is authorized for the current task.
"""
from __future__ import annotations

from itertools import combinations
import platform
import string

ALIASES = {
    "CONTROL": "CTRL", "COMMAND": "CMD", "META": "CMD", "SUPER": "CMD",
    "OPTION": "ALT", "RETURN": "ENTER", "ESCAPE": "ESC", "SPACEBAR": "SPACE",
    "ARROWUP": "UP", "ARROWDOWN": "DOWN", "ARROWLEFT": "LEFT", "ARROWRIGHT": "RIGHT",
    "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "DEL": "DELETE", "BACK": "BACKSPACE",
}
MODIFIERS = ("CTRL", "ALT", "SHIFT", "CMD")
SPECIAL = {"ENTER", "ESC", "TAB", "SPACE", "UP", "DOWN", "LEFT", "RIGHT",
           "HOME", "END", "PAGEUP", "PAGEDOWN", "BACKSPACE", "DELETE", "PLUS"}


def _token(value: str) -> str:
    value = value.strip().upper()
    return ALIASES.get(value, value)


def keyboard_catalog(observation: dict) -> tuple[set[str], tuple[str, ...]]:
    manifest = observation.get("key_capabilities", observation.get("keyboard_capabilities"))
    manifest = manifest if isinstance(manifest, dict) else {}
    is_browser = observation.get("target") == "browser"
    system = str(manifest.get("platform") or observation.get("platform") or platform.system()).lower()
    keys = set(string.ascii_uppercase + string.digits + string.punctuation.replace("+", "")) | SPECIAL
    # Playwright's keyboard map stops at F12 (verified by real headless input).
    # The macOS PyAutoGUI/Quartz map has F1..F20, but no Insert/F21..F24.
    function_max = 12 if is_browser else 20 if system == "darwin" else 24
    keys.update(f"F{i}" for i in range(1, function_max + 1))
    if is_browser or system != "darwin":
        keys.add("INSERT")
    modifiers = MODIFIERS
    supported = manifest.get("supported_keys", manifest.get("keys"))
    if isinstance(supported, list):
        explicit = {_token(value) for value in supported if isinstance(value, str)}
        keys &= explicit
        modifiers = tuple(value for value in modifiers if value in explicit)
    if isinstance(manifest.get("modifiers"), list):
        allowed = {_token(value) for value in manifest["modifiers"] if isinstance(value, str)}
        modifiers = tuple(value for value in modifiers if value in allowed)
    if manifest.get("supports_hotkeys") is False:
        modifiers = ()
    if observation.get("input_transport") == "cua_background_ax_candidate" and not manifest:
        keys &= {"LEFT", "RIGHT", "UP", "DOWN", "TAB", "SPACE", "BACKSPACE", "DELETE", "HOME", "END"}
        modifiers = ()
    return keys, modifiers


def canonical_key_chord(chord: str, observation: dict) -> str:
    if not isinstance(chord, str) or not 1 <= len(chord) <= 100:
        raise ValueError("按鍵必須是已支援的 key 或 hotkey。")
    parts = [_token(value) for value in chord.split("+")]
    if not 1 <= len(parts) <= 5 or any(not part for part in parts) or len(set(parts)) != len(parts):
        raise ValueError("按鍵組合格式無效；不可有空鍵或重複修飾鍵。")
    keys, allowed_modifiers = keyboard_catalog(observation)
    modifiers = [part for part in parts if part in MODIFIERS]
    ordinary = [part for part in parts if part not in MODIFIERS]
    if len(ordinary) > 1 or (ordinary and parts[-1] != ordinary[0]):
        raise ValueError("hotkey 只可包含修飾鍵及最後一個按鍵；文字請使用 type。")
    if any(part not in allowed_modifiers for part in modifiers) or any(part not in keys for part in ordinary):
        raise ValueError("目前操作後端／平台不支援這個按鍵或 hotkey。")
    return "+".join([part for part in MODIFIERS if part in modifiers] + ordinary)


def key_schema(observation: dict) -> dict:
    keys, modifiers = keyboard_catalog(observation)
    chords = set(keys)
    for count in range(1, len(modifiers) + 1):
        for selected in combinations(modifiers, count):
            prefix = "+".join(selected)
            chords.add(prefix)
            chords.update(prefix + "+" + key for key in keys)
    return {"type": "string", "enum": sorted(chords)} if chords else {"not": {}}
