"""Validate generated contracts with an independent JSON Schema implementation."""
import copy
import json

from jsonschema import Draft202012Validator
import pytest

from server.action_space import build_action_space, prepare_model_observation
from server.schemas import Action


def node(id, role, text, x, y, *, source="dom", **extra):
    return {"id": id, "source": source, "role": role, "text": text,
            "x": x, "y": y, "width": 120, "height": 36, **extra}


@pytest.fixture
def observation():
    return {"target": "browser", "width": 800, "height": 600, "elements": [
        node("e_name", "textbox", "Project name", 160, 100, value="Untitled"),
        node("e_choice", "combobox", "Color", 160, 200, options=[
            {"label": "Blue", "value": "blue"}, {"label": "Red", "value": "red"},
            {"label": "Disabled", "value": "disabled", "disabled": True}]),
        node("e_other", "combobox", "Size", 320, 200, options=[{"label": "Large", "value": "large"}]),
        node("e_check", "checkbox", "Notify", 160, 300, checked=False),
        node("e_save", "button", "Save", 160, 400),
        node("e_disabled", "button", "Disabled button", 400, 400, disabled=True),
        node("ocr_save", "text", "Save", 160, 400, source="apple_vision"),
        node("ocr_unique", "text", "Canvas action", 600, 300, source="apple_vision"),
    ], "tabs": [{"id": "tab_1"}, {"id": "tab_2"}]}


def validator(observation):
    schema = build_action_space(prepare_model_observation(observation))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize("action", [
    {"type": "type", "target": "e_name", "text": "A new value"},
    {"type": "select_option", "target": "e_choice", "text": "Red"},
    {"type": "select_option", "target": "e_choice", "text": "blue"},
    {"type": "click", "target": "e_check"},
    {"type": "click", "target": "e_name"},
    {"type": "click", "target": "e_choice"},
    {"type": "click", "target": "e_save"},
    {"type": "click", "target": "ocr_unique"},
    {"type": "click", "x": 200, "y": 220},
    {"type": "type", "x": 200, "y": 220, "text": "coordinate fallback"},
    {"type": "switch_tab", "text": "tab_2"},
    {"type": "key", "key": "CTRL+A"},
    {"type": "drag", "target": "e_name", "end_x": 300, "end_y": 200},
    {"type": "done", "text": "Observed completion evidence"},
])
def test_valid_capability_combinations_also_pass_action_validation(observation, action):
    action = {"reason": "Perform the requested operation", **action}
    validator(observation).validate(action)
    Action.model_validate(action)


@pytest.mark.parametrize("action", [
    {"type": "click", "target": "e_disabled"},
    {"type": "click", "target": "e_previous_frame"},
    {"type": "click", "target": "ocr_save"},  # deduped target is absent from BOTH prompt and schema
    {"type": "click", "target": "e_save", "x": 100, "y": 100},
    {"type": "click", "x": 100},
    {"type": "click", "target": None},
    {"type": "type", "target": "e_name"},
    {"type": "type", "target": "e_name", "text": None},
    {"type": "type", "target": "e_check", "text": "Notify"},
    {"type": "select_option", "target": "e_choice"},
    {"type": "select_option", "target": "e_choice", "text": "Large"},
    {"type": "select_option", "target": "e_choice", "text": "disabled"},
    {"type": "select_option", "target": "e_other", "text": "red"},
    {"type": "select_option", "target": None, "text": "red"},
    {"type": "key"}, {"type": "navigate"}, {"type": "scroll"},
    {"type": "key", "key": "select_option"},
    {"type": "drag", "target": "e_name"}, {"type": "done"},
    {"type": "switch_tab", "text": "unobserved-tab"},
    {"type": "open_app", "app": "Example"},
    {"type": "shell", "text": "anything"},
])
def test_invalid_affordances_and_missing_required_fields_are_not_in_tool_space(observation, action):
    assert not validator(observation).is_valid({"reason": "Public purpose", **action})


def test_dedup_requires_exact_text_and_overlap_and_never_changes_evidence(observation):
    observation["elements"] += [
        node("ocr_distant", "text", "Save", 600, 500, source="rapidocr"),
        node("ocr_different", "text", "Save draft", 160, 400, source="apple_vision"),
        node("ocr_missing_box", "text", "Save", 160, 400, source="apple_vision", width=0),
    ]
    original = copy.deepcopy(observation)
    view = prepare_model_observation(observation)
    ids = {e["id"] for e in view["elements"]}
    assert "ocr_save" not in ids
    assert {"ocr_distant", "ocr_different", "ocr_missing_box"} <= ids
    view["elements"][0]["text"] = "Changed view only"
    assert observation == original


def test_dedup_does_not_hide_ocr_for_a_dom_node_outside_model_view():
    elements = [node("ocr_first", "text", "Save", 160, 400, source="apple_vision")]
    elements += [node(f"e{i}", "button", str(i), i, 100) for i in range(249)]
    elements += [node("outside_limit", "button", "Save", 160, 400)]
    view = prepare_model_observation({"elements": elements})
    assert len(view["elements"]) == 250
    assert view["elements"][0]["id"] == "ocr_first"
    assert not any(e["id"] == "outside_limit" for e in view["elements"])


def test_schema_is_goal_independent_and_does_not_include_field_values_or_oracles(observation):
    a = copy.deepcopy(observation)
    b = copy.deepcopy(observation)
    a.update({"USER_TASK": "Choose Red", "hidden_oracle": "red"})
    b.update({"USER_TASK": "Choose Blue", "hidden_oracle": "blue"})
    a["elements"][0]["value"] = "private initial value"
    assert build_action_space(prepare_model_observation(a)) == build_action_space(prepare_model_observation(b))
    assert "private initial value" not in json.dumps(build_action_space(prepare_model_observation(a)))


def test_desktop_keeps_keyboard_focus_input_and_coordinate_capability():
    obs = {"target": "desktop", "elements": [
        node("ax_name", "TextField", "Name", 100, 100, source="accessibility"),
        node("ax_save", "Button", "Save", 100, 200, source="accessibility"),
    ]}
    v = validator(obs)
    for action in [{"type": "click", "target": "ax_name"},
                   {"type": "type", "target": "ax_name", "text": "new"},
                   {"type": "type", "text": "paste at focus"},
                   {"type": "click", "x": 500, "y": 600},
                   {"type": "open_app", "app": "TextEdit"}]:
        v.validate({"reason": "Public purpose", **action})
    assert not v.is_valid({"reason": "purpose", "type": "type", "target": "ax_save", "text": "wrong"})
    assert not v.is_valid({"reason": "purpose", "type": "new_tab"})


def test_uploads_are_limited_to_observed_file_control_and_allowed_path(observation):
    observation["elements"].append(node("e_file", "textbox", "Attachment", 100, 100, input_type="file"))
    observation["allowed_uploads"] = ["/authorized/example.txt"]
    v = validator(observation)
    action = {"reason": "Attach authorized file", "type": "upload_file", "target": "e_file", "text": "/authorized/example.txt"}
    v.validate(action)
    assert not v.is_valid({**action, "text": "/unrelated/private.txt"})
    assert not v.is_valid({**action, "target": "e_name"})


def test_reason_is_required_brief_public_purpose_and_comes_before_type(observation):
    schema = build_action_space(prepare_model_observation(observation))
    assert all(list(branch["properties"])[:2] == ["reason", "type"] for branch in schema["oneOf"])
    v = Draft202012Validator(schema)
    assert not v.is_valid({"type": "done", "text": "result"})
    assert not v.is_valid({"reason": "x" * 241, "type": "done", "text": "result"})
    assert "thinking" not in json.dumps(schema)


def test_uncertified_backend_empty_manifest_offers_no_input():
    schema = build_action_space({"target": "desktop", "supported_actions": [],
        "input_transport": "cua_background_ax_candidate", "key_capabilities": {
            "backend": "cua_background_ax_candidate", "platform": "darwin",
            "supported_keys": [], "supports_hotkeys": False}})
    assert {item["properties"]["type"]["const"] for item in schema["oneOf"]} == {"wait", "done", "ask_user"}


def test_candidate_single_keys_require_observed_native_field():
    obs = {"target": "desktop", "input_transport": "cua_background_ax_candidate",
        "supported_actions": ["key"], "key_capabilities": {
            "backend": "cua_background_ax_candidate", "platform": "darwin",
            "supported_keys": ["LEFT", "TAB"], "supports_hotkeys": False},
        "elements": [node("ax_text", "TextField", "Name", 100, 100, source="accessibility"),
                     node("ocr_label", "text", "Name", 100, 100, source="apple_vision")]}
    v = validator(obs)
    valid = {"reason": "Move within the field", "type": "key", "target": "ax_text", "key": "LEFT"}
    assert v.is_valid(valid)
    assert not v.is_valid({**valid, "target": "ocr_label"})
    assert not v.is_valid({key: value for key, value in valid.items() if key != "target"})
    assert not v.is_valid({**valid, "key": "CTRL+LEFT"})
