"""Read rendered frame prose for text-only browser agents.

This module only observes Playwright frames. It never executes model-supplied JS
and does not change pages or operate a native desktop.
"""
from __future__ import annotations

from typing import Any

_FRAME_VISIBLE = """el => {
  let box = el.getBoundingClientRect();
  let left = Math.max(0, box.left), top = Math.max(0, box.top);
  let right = Math.min(innerWidth, box.right), bottom = Math.min(innerHeight, box.bottom);
  for (let node = el; node; node = node.parentElement || node.getRootNode().host) {
    const style = getComputedStyle(node);
    if (style.display === 'none' || ['hidden','collapse'].includes(style.visibility)
        || Number(style.opacity) === 0 || style.contentVisibility === 'hidden') return false;
    if (node !== el) {
      const clip = node.getBoundingClientRect();
      if (['hidden','clip','scroll','auto'].includes(style.overflowX)) {
        left = Math.max(left, clip.left); right = Math.min(right, clip.right);
      }
      if (['hidden','clip','scroll','auto'].includes(style.overflowY)) {
        top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom);
      }
    }
  }
  return right > left && bottom > top;
}"""


async def _visible_frame(frame: Any, viewport: dict) -> bool:
    current = frame
    while current.parent_frame is not None:
        host = await current.frame_element()
        try:
            box = await host.bounding_box()
            if not box or (box["x"] + box["width"] <= 0 or box["y"] + box["height"] <= 0
                           or box["x"] >= viewport["width"] or box["y"] >= viewport["height"]):
                return False
            if not await host.evaluate(_FRAME_VISIBLE):
                return False
        finally:
            await host.dispose()
        current = current.parent_frame
    return not frame.is_detached()


async def collect_page_text(page: Any, limit: int = 14000, max_frames: int = 16) -> str:
    """Include visible child-frame prose, with one shared observation text limit.

    Playwright can read each frame independently even across origins. Every
    embedding ancestor is checked; a nested frame inside a hidden parent is
    excluded. As with the existing main-document innerText observation, text
    inside an admitted frame may extend beyond that frame's current scroll area.
    """
    if limit <= 0:
        return ""
    sections: list[str] = []
    viewport = page.viewport_size or {"width": 1280, "height": 800}
    for index, frame in enumerate(page.frames[:max_frames + 1]):
        try:
            if frame != page.main_frame and not await _visible_frame(frame, viewport):
                continue
            content = (await frame.locator("body").inner_text(timeout=750)).strip()
            if content:
                sections.append(content if frame == page.main_frame else f"[Frame {index}]\n{content}")
        except Exception:
            continue  # A page may detach or navigate a frame during observation.
    return "\n\n".join(sections)[:limit]
