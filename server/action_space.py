"""Build a typed action space from observed capabilities, never from a task.

This module has no provider, planner, driver, policy, or task-state dependency.
It is suitable for an adapter/tool manifest behind a single ActionGateway.
An allowed action remains a proposal: this schema grants no execution permission
and never decides whether an action or task succeeded.
"""
from __future__ import annotations

import copy
import math
import re

from .schemas import Action
from .key_contract import key_schema

MAX_ELEMENTS = 250
_OCR = {"apple_vision", "rapidocr"}
_TEXT_ROLES = {"textbox", "searchbox", "spinbutton", "textfield", "textarea", "searchfield", "combobox"}
_NON_TEXT_INPUTS = {"checkbox", "radio", "file", "button", "submit", "reset", "image", "hidden"}
_CLICK_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "treeitem", "option"}
_ACTION_PROPERTIES = Action.model_json_schema()["properties"]


def _box(element: dict) -> tuple[float, float, float, float] | None:
    try:
        x, y, width, height = (float(element[key]) for key in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0:
        return None
    return x - width / 2, y - height / 2, x + width / 2, y + height / 2


def _duplicate_ocr(ocr: dict, dom: dict) -> bool:
    # Exact normalized text AND substantial overlap, not a nearby matching word.
    text = " ".join(str(ocr.get("text", "")).split())
    if not text or text != " ".join(str(dom.get("text", "")).split()):
        return False
    a, b = _box(ocr), _box(dom)
    if not a or not b:
        return False
    overlap = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return overlap / ((a[2] - a[0]) * (a[3] - a[1])) >= 0.8


def _is_text_target(element: dict) -> bool:
    if element.get("disabled") or element.get("input_type") in _NON_TEXT_INPUTS:
        return False
    role = str(element.get("role", "")).lower()
    if element.get("source") == "dom":
        if isinstance(element.get("options"), list) or role in _CLICK_ROLES:
            return False
        return role in _TEXT_ROLES or "value" in element  # includes contenteditable DOM nodes
    return role in _TEXT_ROLES or element.get("source") in _OCR or element.get("source") == "yolo_world"


def _click_target(element: dict) -> bool:
    # Focusing textboxes and opening comboboxes are valid clicks. Restricting
    # these can force a weak model's intended focus click onto a wrong button.
    return not element.get("disabled")


def prepare_model_observation(observation: dict) -> dict:
    """Copy/minimize the model view; original evidence and driver targets survive."""
    result = dict(observation)
    original = observation.get("elements", [])
    original = original if isinstance(original, list) else []
    valid = [element for element in original if isinstance(element, dict)
             and isinstance(element.get("id"), str)
             and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", element["id"])][:MAX_ELEMENTS]
    dom = [element for element in valid if element.get("source") == "dom"]
    selected, seen = [], set()
    for element in valid:
        if element["id"] in seen:
            continue
        if element.get("source") in _OCR and any(_duplicate_ocr(element, node) for node in dom):
            continue
        selected.append(copy.deepcopy(element))
        seen.add(element["id"])
        if len(selected) == MAX_ELEMENTS:
            break
    result["elements"] = selected
    return result


def _field(name: str) -> dict:
    def clean(value):
        if isinstance(value, dict):
            if "anyOf" in value:
                non_null = [item for item in value["anyOf"] if item.get("type") != "null"]
                if len(non_null) == 1:
                    return clean(non_null[0])
            return {key: clean(item) for key, item in value.items() if key not in {"maxLength", "title", "default"}}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return clean(_ACTION_PROPERTIES[name])


def build_action_space(model_observation: dict) -> dict:
    """JSON Schema of currently available operations, independent of goal text.

    Pass exactly the prepared observation that will be sent to the model.
    Semantic targets have capability-specific actions; coordinate alternatives
    retain low-level computer use when semantic controls are insufficient.
    """
    elements = model_observation.get("elements", [])[:MAX_ELEMENTS]
    enabled = [element for element in elements if not element.get("disabled")]
    mode = model_observation.get("target")
    browser = mode == "browser"
    desktop = mode == "desktop"
    branches = []

    def branch(kind: str, fields: dict | None = None, required: tuple = ()):
        # A short, public action purpose comes before choosing the operation.
        # This is not a request for private reasoning or a multi-step plan.
        properties = {"reason": {"type": "string", "minLength": 1, "maxLength": 240},
                      "type": {"const": kind}}
        properties.update(fields or {})
        properties.update({"expected": _field("expected"), "memory": _field("memory")})
        branches.append({"type": "object", "properties": properties,
                         "required": ["reason", "type", *required], "additionalProperties": False})

    def ids(nodes):
        return {"type": "string", "enum": list(dict.fromkeys(node["id"] for node in nodes))}

    for kind in ("click", "double_click", "move", "drag"):
        eligible = [node for node in enabled if _click_target(node)] if kind in {"click", "double_click"} else enabled
        extra = {"button": _field("button")} if kind == "click" else {}
        required = ()
        if kind == "drag":
            extra = {name: _field(name) for name in ("end_x", "end_y", "duration")}
            required = ("end_x", "end_y")
        if eligible:
            branch(kind, {"target": ids(eligible), **extra}, ("target", *required))
        branch(kind, {"x": _field("x"), "y": _field("y"), **extra}, ("x", "y", *required))

    text_targets = [node for node in enabled if _is_text_target(node)]
    if text_targets:
        branch("type", {"target": ids(text_targets), "text": _field("text")}, ("target", "text"))
    branch("type", {"x": _field("x"), "y": _field("y"), "text": _field("text")}, ("x", "y", "text"))
    # Desktop paste/keyboard sequences and visual-only input can use current focus.
    if not browser or not any(node.get("source") == "dom" for node in text_targets):
        branch("type", {"text": _field("text")}, ("text",))

    if browser:
        for node in enabled:
            if node.get("source") != "dom" or str(node.get("role", "")).lower() != "combobox":
                continue
            options = node.get("options")
            if not isinstance(options, list):
                continue
            choices = list(dict.fromkeys(option[key] for option in options[:80]
                if isinstance(option, dict) and not option.get("disabled")
                for key in ("label", "value") if isinstance(option.get(key), str) and len(option[key]) <= 1000))
            if choices:
                branch("select_option", {"target": {"const": node["id"]}, "text": {"type": "string", "enum": choices}}, ("target", "text"))
        files = [value for value in model_observation.get("allowed_uploads", []) if isinstance(value, str) and 0 < len(value) <= 12000]
        file_targets = [node for node in enabled if node.get("source") == "dom" and node.get("input_type") == "file"]
        if files and file_targets:
            branch("upload_file", {"target": ids(file_targets), "text": {"type": "string", "enum": files}}, ("target", "text"))
        branch("back")
        branch("forward")
        branch("new_tab", {"url": _field("url")})
        tabs = [tab["id"] for tab in model_observation.get("tabs", []) if isinstance(tab, dict) and isinstance(tab.get("id"), str)]
        if tabs:
            branch("switch_tab", {"text": {"type": "string", "enum": tabs}}, ("text",))
            branch("close_tab", {"text": {"type": "string", "enum": tabs}})
        else:
            branch("close_tab")
    if desktop:
        branch("open_app", {"app": {"type": "string", "minLength": 1}}, ("app",))
    branch("navigate", {"url": {"type": "string", "minLength": 1}}, ("url",))
    keyboard_schema = key_schema(model_observation)
    if keyboard_schema.get("enum"):
        if model_observation.get("input_transport") == "cua_background_ax_candidate":
            # The candidate Cua path only accepts keys bound to native fields.
            native_fields = [node for node in text_targets if node.get("source") == "accessibility"]
            if native_fields:
                branch("key", {"target": ids(native_fields), "key": keyboard_schema}, ("target", "key"))
        else:
            branch("key", {"key": keyboard_schema}, ("key",))
    branch("scroll", {"direction": _field("direction"), "amount": _field("amount")}, ("direction",))
    branch("wait", {"seconds": _field("seconds")})
    for kind in ("done", "ask_user"):
        branch(kind, {"text": {"type": "string", "minLength": 1}}, ("text",))
    if isinstance(model_observation.get("supported_actions"), list):
        allowed = set(model_observation["supported_actions"]) | {"wait", "done", "ask_user"}
        branches = [item for item in branches if item["properties"]["type"]["const"] in allowed]
    return {"oneOf": branches}
