"""Refresh human-approved targets without silently reusing stale coordinates."""
from __future__ import annotations

import base64
from io import BytesIO

from PIL import Image, ImageChops, ImageStat

from .schemas import Action


class StaleObservation(ValueError):
    pass


def _image(value: str, mode: str = "L") -> Image.Image:
    data = base64.b64decode(value.split(",", 1)[1])
    with Image.open(BytesIO(data)) as image:
        return image.convert(mode)


def _verify_coordinate_regions(action: Action, before: dict, after: dict) -> None:
    points = []
    if action.type in ("click", "double_click", "type", "move", "drag") and not action.target and action.x is not None:
        points.append((action.x, action.y))
    if action.type == "drag":
        # The destination is always a raw coordinate, even for a semantic start.
        points.append((action.end_x, action.end_y))
    if not points or not before.get("image") or not after.get("image"):
        return
    a, b = _image(before["image"], "RGB"), _image(after["image"], "RGB")
    if a.size != b.size or a.size != (before["width"], before["height"]):
        raise StaleObservation("操作座標與畫面尺寸不一致")
    width, height = a.size
    for x, y in points:
        if x is None or y is None or not (0 <= x < width and 0 <= y < height):
            raise StaleObservation("操作座標已超出畫面")
        # A 64x64 neighborhood catches a replaced small icon that disappears in
        # a full-screen thumbnail. Clip to real pixels, not padded image space.
        radius = 32
        box = (max(0, int(x) - radius), max(0, int(y) - radius),
               min(width, int(x) + radius), min(height, int(y) + radius))
        channels = ImageChops.difference(a.crop(box), b.crop(box)).split()
        maximum = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
        # Max RGB channel difference preserves equal-luminance color changes.
        # Ignore low-amplitude antialiasing and at most 2% isolated changed pixels.
        changed = sum(maximum.histogram()[21:])
        if changed / (maximum.width * maximum.height) > 0.02:
            raise StaleObservation("操作座標附近的可見內容已變更")


def remap_approved_action(action: Action, before: dict, after: dict) -> Action:
    if (before.get("width"), before.get("height"), before.get("title"), before.get("url")) != (
        after.get("width"), after.get("height"), after.get("title"), after.get("url")
    ):
        raise StaleObservation("視窗或頁面已切換")
    _verify_coordinate_regions(action, before, after)
    if action.target:
        old = next((e for e in before.get("elements", []) if e.get("id") == action.target), None)
        if old is None:
            raise StaleObservation("原操作元素已失效")
        matching = []
        for new in after.get("elements", []):
            if any(old.get(k) != new.get(k) for k in ("source", "role", "text", "value", "href", "checked", "disabled")):
                continue
            if any(abs(float(old.get(k, 0)) - float(new.get(k, 0))) > 3 for k in ("x", "y", "width", "height")):
                continue
            matching.append(new)
        if len(matching) != 1:
            raise StaleObservation("畫面元素已移動、變更或不再唯一")
        if before.get("image") and after.get("image"):
            # A native AX element may still exist while another window covers it.
            # Its visible region must also agree; semantic identity alone is weak.
            x, y = old.get("x", 0), old.get("y", 0)
            w, h = max(old.get("width", 0), 8), max(old.get("height", 0), 8)
            box = (max(0, int(x - w / 2)), max(0, int(y - h / 2)), min(before["width"], int(x + w / 2)), min(before["height"], int(y + h / 2)))
            a, b = _image(before["image"]).crop(box), _image(after["image"]).crop(box)
            if ImageStat.Stat(ImageChops.difference(a, b)).mean[0] > 8:
                raise StaleObservation("操作目標的可見內容已變更或被遮住")
        return action.model_copy(update={"target": matching[0]["id"]})
    if action.type in ("navigate", "open_app", "new_tab", "wait"):
        return action
    # Keyboard actions and bare coordinates have no semantic target to match.
    # Compare both visible content and a downsampled frame, tolerating tiny cursor
    # or antialias changes but rejecting materially different screens.
    if before.get("_grounding_text", before.get("text")) != after.get("_grounding_text", after.get("text")):
        raise StaleObservation("等待確認期間畫面文字已變更")
    if before.get("image") and after.get("image"):
        def thumbnail(value):
            return _image(value).resize((160, 100))
        diff = ImageChops.difference(thumbnail(before["image"]), thumbnail(after["image"]))
        if ImageStat.Stat(diff).mean[0] > 2.5:
            raise StaleObservation("等待確認期間畫面已變更")
    return action
