import copy

from server.feedback import visible_effect, action_fingerprint
from server.schemas import Action


def frame(**state):
    return {"text": "form", "elements": [{"id": "e1", "role": "checkbox", "source": "dom", "x": 50, "y": 50, "width": 10, "height": 10, "text": "Notifications", **state}]}


def test_feedback_distinguishes_click_from_actual_checked_change():
    before = frame(checked=False)
    after = frame(checked=True)
    after["elements"][0]["id"] = "e99"
    result = visible_effect(before, after, Action(type="click", target="e1"))
    assert '"checked": true' in result
    assert '"target_state_changed": true' in result
    unchanged = visible_effect(before, before, Action(type="click", target="e1"))
    assert '"target_state_changed": false' in unchanged


def test_feedback_does_not_guess_when_target_replaced_or_ambiguous():
    before = frame(checked=False)
    after = frame(checked=True)
    after["elements"].append(copy.deepcopy(after["elements"][0]))
    result = visible_effect(before, after, Action(type="click", target="e1"))
    assert "no longer uniquely matches" in result
    assert "target_now" not in result


def test_feedback_masks_password_and_reports_navigation():
    before = frame(input_type="password", value="old-secret")
    after = frame(input_type="password", value="new-secret")
    after["url"] = "https://example.test/next"
    result = visible_effect(before, after, Action(type="type", target="e1", text="new-secret"))
    assert "secret" not in result
    assert "https://example.test/next" in result


def test_loop_fingerprint_ignores_frame_ids_but_respects_actual_state():
    first = frame(checked=False)
    next_frame = frame(checked=False)
    next_frame["elements"][0]["id"] = "e99"
    a = action_fingerprint(Action(type="click", target="e1", reason="first"), first)
    b = action_fingerprint(Action(type="click", target="e99", reason="changed reason"), next_frame)
    assert a == b
    next_frame["elements"][0]["checked"] = True
    assert b != action_fingerprint(Action(type="click", target="e99"), next_frame)


def test_different_navigation_and_scroll_are_not_counted_as_same_loop():
    assert action_fingerprint(Action(type="navigate", url="https://example.test/a"), frame()) != action_fingerprint(Action(type="navigate", url="https://example.test/b"), frame())
    assert action_fingerprint(Action(type="scroll", direction="up"), frame()) != action_fingerprint(Action(type="scroll", direction="down"), frame())
