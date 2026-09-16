"""An executor-owned Playwright pointer, never a physical desktop input device."""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from .motion import MotionPolicy, trajectory

# The empty, aria-hidden host is outside body text. Closed shadow content has
# no labels/controls and never enters DOM target enumeration. No page callback
# is trusted as the source of cursor position: Python updates it only after an
# actual Playwright mouse.move has completed.
_CREATE_OVERLAY = """() => {
  const host=document.createElement('div');
  host.setAttribute('data-computeruse-cursor',''); host.setAttribute('aria-hidden','true');
  host.style.cssText='position:fixed!important;left:0!important;top:0!important;width:22px!important;height:30px!important;pointer-events:none!important;z-index:2147483647!important;display:block!important;opacity:1!important;';
  const shadow=host.attachShadow({mode:'closed'});
  // Construct fixed SVG nodes without an HTML injection sink. Sites can
  // enforce Trusted Types and forbid policy creation; preserve those rules.
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('width','22'); svg.setAttribute('height','30');
  svg.setAttribute('viewBox','0 0 22 30');
  svg.style.cssText='display:block;pointer-events:none';
  const path=document.createElementNS('http://www.w3.org/2000/svg','path');
  path.setAttribute('d','M2 2 L2 23 L7 18 L11 27 L15 25 L11 16 L19 16 Z');
  path.setAttribute('fill','#8b5cf6'); path.setAttribute('stroke','white');
  path.setAttribute('stroke-width','2'); path.setAttribute('stroke-linejoin','round');
  svg.append(path); shadow.append(svg);
  document.documentElement.append(host);
  return {host,document};
}"""


class BrowserPointer:
    def __init__(self, page: Any, interrupted: asyncio.Event, boundary, fatal_error, *, policy=None):
        self.page = page
        self.policy = policy or MotionPolicy()
        self.interrupted, self.boundary, self.fatal_error = interrupted, boundary, fatal_error
        self.position = (0, 0)
        self.samples = 0
        self._overlay = None
        self._held: set[str] = set()

    async def overlay(self):
        if self._overlay is not None:
            try:
                if not await self._overlay.evaluate('s => s.document===document && s.host.isConnected'):
                    self._overlay = None
            except Exception:
                self._overlay = None
        if self._overlay is None:
            self._overlay = await self.page.evaluate_handle(_CREATE_OVERLAY)
        await self._overlay.evaluate('(s,p) => {s.host.style.setProperty("transform",`translate(${p[0]}px,${p[1]}px)`,"important")}', list(self.position))

    async def delay(self, seconds: float):
        self.boundary()
        if seconds > 0:
            try:
                await asyncio.wait_for(self.interrupted.wait(), timeout=seconds)
            except TimeoutError:
                pass
        self.boundary()

    async def move(self, point: tuple[int, int], duration: float | None = None):
        self.boundary()
        began = time.monotonic()
        for sample in trajectory(self.position, point, self.policy, duration):
            await self.delay(max(0, began + sample.seconds - time.monotonic()))
            self.boundary()
            await self.page.mouse.move(sample.x, sample.y)
            self.position = (sample.x, sample.y)
            self.samples += 1
            await self.overlay()
        self.boundary()

    async def down(self, button: str, count: int = 1):
        self.boundary()
        # A command can reach Chromium before its await fails; release even in
        # that unknown case. There is never a retry of an unknown mouse-down.
        self._held.add(button)
        await self.page.mouse.down(button=button, click_count=count)

    async def up(self, button: str, count: int = 1):
        if button not in self._held:
            return
        async def release():
            try:
                await self.page.mouse.up(button=button, click_count=count)
            except Exception as exc:
                if not self.page.is_closed():
                    raise self.fatal_error('瀏覽器滑鼠按鍵無法釋放；已停止工作。') from exc
            finally:
                self._held.discard(button)
        task = asyncio.create_task(release())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def click(self, point, *, button='left', count=1, validate=None):
        await self.move(point)
        await self.delay(self.policy.settle_seconds)
        for index in range(1, count + 1):
            self.boundary()
            if validate is not None:
                await validate(point)
            self.boundary()
            try:
                await self.down(button, index)
                await self.delay(0.035)
            finally:
                await self.up(button, index)
            if index < count:
                await self.delay(self.policy.double_click_gap)

    async def drag(self, start, end, *, button='left', duration=None, validate=None):
        await self.move(start)
        await self.delay(self.policy.settle_seconds)
        if validate is not None:
            await validate(start)
        try:
            await self.down(button)
            await self.move(end, duration)
        finally:
            await self.up(button)

    async def close(self):
        for button in tuple(self._held):
            await self.up(button)
        if self._overlay is not None:
            try:
                await self._overlay.dispose()
            except Exception:
                pass
        self._overlay = None

    def metadata(self) -> dict:
        x, y = self.position
        return {'x': x, 'y': y, 'width': 22, 'height': 30,
                'transport': 'playwright_page_mouse', 'physical_os_pointer': False,
                'samples_sent': self.samples, 'pressed_buttons': sorted(self._held)}


async def handle_point(handle, viewport: dict) -> tuple[int, int]:
    box = await handle.bounding_box()
    if not box or not await handle.is_visible() or not await handle.is_enabled():
        raise ValueError('目標已隱藏、停用或移除；請重新觀察。')
    left, top = max(0, box['x']), max(0, box['y'])
    right = min(viewport['width'], box['x'] + box['width'])
    bottom = min(viewport['height'], box['y'] + box['height'])
    if right - left < 1 or bottom - top < 1:
        raise ValueError('目標不在目前可見範圍；請先捲動或重新觀察。')
    return math.floor((left+right)/2), math.floor((top+bottom)/2)


async def validate_hit(handle, point):
    """Check this node and every iframe host at the executor's actual point."""
    current = handle
    owned = False
    try:
        while current is not None:
            box = await current.bounding_box()
            if not box or not (box['x'] <= point[0] < box['x']+box['width'] and box['y'] <= point[1] < box['y']+box['height']):
                raise ValueError('目標在游標移動期間改變位置；未按下滑鼠，請重新觀察。')
            visible_hit = await current.evaluate('''(el,p) => {
              if(!el.isConnected || el.disabled) return false;
              const r=el.getBoundingClientRect();
              const hit=el.ownerDocument.elementFromPoint(r.left+p[0],r.top+p[1]);
              return !!hit && (hit===el || el.contains(hit));
            }''', [point[0]-box['x'],point[1]-box['y']])
            if not visible_hit:
                raise ValueError('目標被其他元素遮住；未按下滑鼠，請重新觀察。')
            frame = await current.owner_frame()
            if owned:
                await current.dispose()
                owned = False
            if frame is None or frame.parent_frame is None:
                break
            current = await frame.frame_element()
            owned = True
    finally:
        if owned:
            await current.dispose()
