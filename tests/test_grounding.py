import pytest
import base64
from io import BytesIO
from PIL import Image, ImageDraw

from server.grounding import StaleObservation, remap_approved_action
from server.schemas import Action


def observation(id="e1", **updates):
    return {"title": "test", "url": "https://example.com", "width": 1280, "height": 800, "text": "Save", "elements": [{"id": id, "source": "dom", "role": "button", "text": "Save", "x": 150, "y": 100, "width": 80, "height": 40, **updates}]}


def test_approval_refresh_remaps_only_same_semantic_target():
    action = remap_approved_action(Action(type="click", target="e1"), observation(), observation("e9"))
    assert action.target == "e9"
    for changes in [{"text": "Delete"}, {"x": 300}, {"disabled": True}, {"href": "https://attacker.example"}]:
        with pytest.raises(StaleObservation):
            remap_approved_action(Action(type="click", target="e1"), observation(), observation("e9", **changes))


def test_coordinate_approval_rejects_changed_screen_context():
    before = observation()
    after = {**before, "title": "other app"}
    with pytest.raises(StaleObservation):
        remap_approved_action(Action(type="key", key="CMD+V"), before, after)


def test_accessibility_target_covered_by_other_window_is_rejected():
    def frame(covered):
        image = Image.new("RGB", (1280, 800), "white")
        if covered:
            ImageDraw.Draw(image).rectangle((110, 80, 190, 120), fill="black")
        out = BytesIO(); image.save(out, format="PNG")
        return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
    before, after = observation(), observation("e2")
    before["image"], after["image"] = frame(False), frame(True)
    with pytest.raises(StaleObservation, match="遮住"):
        remap_approved_action(Action(type="click", target="e1"), before, after)


def image_observation(image, id="e1"):
    out = BytesIO()
    image.save(out, format="PNG")
    return {**observation(id), "image": "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()}


def icon_frame(color, center=(500, 500)):
    image = Image.new("RGB", (1280, 800), "white")
    x, y = center
    ImageDraw.Draw(image).rectangle((x - 10, y - 10, x + 10, y + 10), fill=color)
    return image


def raw_action(kind, **updates):
    data = {"type": kind, "x": 500, "y": 500}
    if kind == "type":
        data["text"] = "example"
    if kind == "drag":
        data.update(end_x=900, end_y=500)
    return Action(**{**data, **updates})


@pytest.mark.parametrize("kind", ["click", "double_click", "type", "move", "drag"])
def test_raw_coordinate_rejects_small_replaced_icon_despite_same_screen_text(kind):
    before = image_observation(icon_frame("black"))
    after = image_observation(icon_frame("red"))
    with pytest.raises(StaleObservation, match="座標附近"):
        remap_approved_action(raw_action(kind), before, after)


def test_raw_coordinate_rejects_different_colors_with_identical_grayscale():
    before_image = icon_frame((255, 0, 0))
    after_image = icon_frame((0, 130, 0))
    # Grayscale comparison, including the old full-frame check, cannot see this.
    assert before_image.convert("L").tobytes() == after_image.convert("L").tobytes()
    with pytest.raises(StaleObservation, match="座標附近"):
        remap_approved_action(raw_action("click"), image_observation(before_image), image_observation(after_image))


@pytest.mark.parametrize("semantic_start", [False, True])
def test_drag_rejects_changed_destination_with_unchanged_start(semantic_start):
    before = image_observation(icon_frame("black", center=(900, 500)))
    after = image_observation(icon_frame("red", center=(900, 500)))
    action = Action(type="drag", target="e1", end_x=900, end_y=500) if semantic_start else raw_action("drag")
    with pytest.raises(StaleObservation, match="座標附近"):
        remap_approved_action(action, before, after)


@pytest.mark.parametrize("kind", ["click", "double_click", "type", "move", "drag"])
def test_unchanged_canvas_allows_raw_coordinates(kind):
    frame = image_observation(icon_frame("black"))
    action = raw_action(kind)
    assert remap_approved_action(action, frame, frame) == action


@pytest.mark.parametrize("change", ["low_amplitude", "sparse"])
def test_raw_coordinate_tolerates_small_antialiasing_changes(change):
    before = Image.new("RGB", (1280, 800), (100, 100, 100))
    after = before.copy()
    if change == "low_amplitude":
        ImageDraw.Draw(after).rectangle((468, 468, 531, 531), fill=(120, 100, 100))
    else:
        # 80 high-contrast pixels / 4096 pixels = 1.95%, below the 2% limit.
        ImageDraw.Draw(after).rectangle((496, 495, 503, 504), fill=(255, 255, 255))
    action = raw_action("click")
    assert remap_approved_action(action, image_observation(before), image_observation(after)) == action


def test_raw_coordinate_uses_actual_crop_area_at_screen_edge():
    before = Image.new("RGB", (1280, 800), "white")
    after = before.copy()
    # 25 pixels / the clipped 32x32 crop exceeds 2%; / 64x64 would miss it.
    ImageDraw.Draw(after).rectangle((0, 0, 4, 4), fill="black")
    with pytest.raises(StaleObservation, match="座標附近"):
        remap_approved_action(raw_action("click", x=0, y=0), image_observation(before), image_observation(after))


def test_raw_coordinate_retains_global_frame_check_away_from_target():
    before = Image.new("RGB", (1280, 800), "white")
    after = before.copy()
    ImageDraw.Draw(after).rectangle((0, 0, 1279, 200), fill="black")
    with pytest.raises(StaleObservation, match="等待確認期間畫面已變更"):
        remap_approved_action(raw_action("click"), image_observation(before), image_observation(after))


def test_raw_coordinate_rejects_image_dimension_mismatch():
    before = image_observation(Image.new("RGB", (1280, 800), "white"))
    after = image_observation(Image.new("RGB", (640, 400), "white"))
    with pytest.raises(StaleObservation, match="尺寸不一致"):
        remap_approved_action(raw_action("click"), before, after)
