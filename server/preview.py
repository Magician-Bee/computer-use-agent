"""Read-only live frames from the agent's Chromium, separate from model input."""
from __future__ import annotations

import asyncio
import time


class BrowserPreview:
    def __init__(self, run):
        self.run = run
        self._lock = asyncio.Lock()
        self._at = 0.0
        self._frame: tuple[bytes, str] | None = None

    async def frame(self) -> tuple[bytes, str] | None:
        if self.run.request.target != "browser":
            return self.run.screenshot()
        async with self._lock:
            if self._frame and time.monotonic() - self._at < 0.15:
                return self._frame
            # Only read the driver's already-selected page. Do not select tabs,
            # bring a window forward, run observation/DOM code, or change focus.
            page = getattr(self.run.driver, "_page", None)
            if page is not None and not page.is_closed() and not getattr(self.run.driver, "_closing", False):
                try:
                    picture = await page.screenshot(type="jpeg", quality=70, timeout=1800)
                    self._frame = picture, "image/jpeg"
                    self._at = time.monotonic()
                    return self._frame
                except Exception:
                    pass  # Closing/navigation may invalidate a read-only capture.
            return self.run.screenshot()

    async def stream(self, disconnected):
        try:
            while not await disconnected():
                picture = await self.frame()
                if picture:
                    raw, content_type = picture
                    yield (b"--computeruse-frame\r\nContent-Type: " + content_type.encode("ascii")
                           + b"\r\nContent-Length: " + str(len(raw)).encode("ascii") + b"\r\n\r\n" + raw + b"\r\n")
                if self.run.status not in {"starting", "running", "awaiting_approval", "paused", "awaiting_input"}:
                    break
                await asyncio.sleep(0.18)
        finally:
            # No input or browser lifetime belongs to a preview connection.
            pass
