"""Pinned Cua MCP adapter for explicitly selected windows.

This is a restricted background candidate, not a certified co-working backend.
It has no PyAutoGUI, foreground escalation, global keys, or clipboard fallback.
Production capabilities remain unavailable until independent concurrency tests
certify the supported app/action matrix. See docs/cua-audit.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import platform
import uuid
from typing import Any

from .drivers import ActionInterrupted, DriverAbort, _Targets, _keys

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.26.1"
SOURCE_COMMIT = "cc54254464c0c9aebfd6547fe7e4a0ceaf0456d7"
BINARY = ROOT / ".tools/cua-driver/0.26.1/cua-driver-rs-0.26.1-darwin-arm64/CuaDriver.app/Contents/MacOS/cua-driver"
BINARY_SHA256 = "51b7515e5b124c179f0be56ef87ae5ede226e4a702e0d335b04ed2edd91c03c6"
_CANDIDATE_ACTIONS = frozenset({"click", "type", "key"})
_CLICK_ROLES = frozenset({"button", "checkbox", "radiobutton"})
_TEXT_ROLES = frozenset({"textfield", "textarea", "combobox"})
_EDITING_KEYS = frozenset({"left", "right", "up", "down", "tab", "space", "backspace", "delete", "home", "end"})


async def capabilities() -> dict:
    installed = BINARY.is_file()
    return {"available": False, "backend": "cua", "physical_input_untouched": False,
            "installed": installed, "version": VERSION if installed else None,
            "mode": "background_ax_candidate", "tested_actions": [],
            "agent_cursor_available": False,
            "reason": ("Cua 已隔離安裝；共用桌面的焦點、鍵盤及剪貼簿互不干擾仍待獨立驗證。"
                       if installed else "尚未安裝已核對版本的 Cua 背景驅動。")}


def _verified_binary() -> Path:
    if platform.system() != "Darwin" or platform.machine() not in {"arm64", "aarch64"}:
        raise RuntimeError("目前這個 Cua 候選版本只完成 macOS Apple Silicon 安裝核對。")
    if not BINARY.is_file() or hashlib.sha256(BINARY.read_bytes()).hexdigest() != BINARY_SHA256:
        raise RuntimeError("Cua 驅動不存在或雜湊不符；不會改用實體滑鼠／鍵盤。")
    return BINARY


class _McpTransport:
    """Own the SDK contexts in one task; cancellation closes only our process."""

    def __init__(self):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._runner: asyncio.Task | None = None
        self._ready: asyncio.Future | None = None

    async def start(self):
        if self._runner is not None:
            return
        binary = await asyncio.to_thread(_verified_binary)
        if importlib.util.find_spec("mcp") is None:
            raise RuntimeError("Cua 背景連線需要安裝 Python mcp 套件。")
        self._ready = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._serve(binary))
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), 15)
        except BaseException:
            await self.close()
            raise

    async def _serve(self, binary):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ}
        env.update(CUA_DRIVER_RS_TELEMETRY_ENABLED="0", CUA_DRIVER_RS_UPDATE_CHECK="0",
                   CUA_DRIVER_PERMISSION_MODE="standard")
        # Deliberately read-only discovery/observation: direct mode has no
        # macOS overlay. It is not the formal co-working action transport.
        parameters = StdioServerParameters(command=str(binary), args=["mcp", "--direct"], env=env)
        active = None
        try:
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    names = {tool.name for tool in listing.tools}
                    if not {"get_window_state", "list_windows", "click", "type_text", "press_key"} <= names:
                        raise RuntimeError("Cua MCP 工具介面不符已核對版本。")
                    self._ready.set_result(None)
                    while True:
                        name, arguments, active = await self._queue.get()
                        if active.cancelled():
                            active = None
                            continue
                        try:
                            result = await session.call_tool(name, arguments)
                            if not active.done():
                                active.set_result(result)
                        except Exception as exc:
                            if not active.done():
                                active.set_exception(exc)
                        except BaseException:
                            if not active.done():
                                active.set_exception(RuntimeError("Cua 連線已中止；已送出的動作結果需要重新確認。"))
                            raise
                        finally:
                            active = None
        except BaseException as exc:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(exc)
            if active is not None and not active.done():
                active.set_exception(RuntimeError("Cua 連線已中止；已送出的動作結果需要重新確認。"))
            raise
        finally:
            while not self._queue.empty():
                _, _, pending = self._queue.get_nowait()
                if not pending.done():
                    pending.set_exception(RuntimeError("Cua 連線已關閉。"))

    async def call(self, name: str, arguments: dict):
        if self._runner is None or self._runner.done():
            raise RuntimeError("Cua MCP 連線尚未啟動或已關閉。")
        future = asyncio.get_running_loop().create_future()
        await self._queue.put((name, arguments, future))
        return await future

    async def close(self):
        task, self._runner = self._runner, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def interrupt(self):
        # Cancelling the request Future alone does not stop a stdio server.
        # Cancel the owning context so its child is terminated and reaped.
        if self._runner is not None:
            self._runner.cancel()


def _result_parts(result: Any) -> tuple[dict, list[dict]]:
    if hasattr(result, "model_dump"):
        result = result.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(result, dict):
        raise RuntimeError("Cua 回傳格式無效。")
    blocks = result.get("content", [])
    if result.get("isError"):
        text = " ".join(str(block.get("text", "")) for block in blocks if isinstance(block, dict))
        raise RuntimeError("Cua 拒絕動作：" + text[:800])
    structured = result.get("structuredContent")
    if not isinstance(structured, dict):
        raise RuntimeError("Cua 缺少已核對的 structuredContent；不從非結構文字猜測元素。")
    return structured, blocks


async def list_windows() -> list[dict]:
    transport = _McpTransport()
    try:
        await transport.start()
        result = await asyncio.wait_for(transport.call("list_windows", {"on_screen_only": True}), 12)
        structured, _ = _result_parts(result)
        windows = structured.get("windows")
        if not isinstance(windows, list):
            raise RuntimeError("Cua 視窗清單格式無效。")
        return [{key: item[key] for key in ("pid", "window_id", "app_name", "title", "bounds", "is_on_screen") if key in item}
                for item in windows if isinstance(item, dict)
                and type(item.get("pid")) is int and item["pid"] > 0
                and type(item.get("window_id")) is int and item["window_id"] > 0]
    finally:
        await transport.close()


class CuaDriver(_Targets):
    """Exact-window, background-only candidate with explicit action admission.

    enabled_actions is an internal integration/test gate, not a model argument
    or a claim of certification. The production factory leaves it empty until
    concurrency evidence covers the intended target and actions.
    """

    def __init__(self, pid: int | None = None, window_id: int | None = None,
                 *, enabled_actions=(), _transport=None):
        super().__init__()
        if not set(enabled_actions) <= _CANDIDATE_ACTIONS:
            raise ValueError("此候選背景驅動不支援要求的輸入方式。")
        self.pid, self.window_id = pid, window_id
        self.enabled_actions = frozenset(enabled_actions)
        self._transport = _transport or _McpTransport()
        self._tokens: dict[str, dict] = {}
        self._generation = 0
        self._interrupted = False
        self._closed = False
        self._inflight: asyncio.Task | None = None
        self._session = "computeruse-" + uuid.uuid4().hex[:16]
        self.downloads, self.download_paths = [], {}

    async def start(self):
        if type(self.pid) is not int or self.pid <= 0 or type(self.window_id) is not int or self.window_id <= 0:
            raise ValueError("請先明確選擇代理要操作的應用程式視窗；不會接管目前前景視窗。")
        await self._transport.start()

    async def _call(self, tool: str, arguments: dict, *, timeout: float = 25):
        if self._closed:
            raise DriverAbort("背景工作階段已關閉。")
        if self._interrupted:
            raise ActionInterrupted("背景動作已中斷；恢復後需重新觀察。")
        task = asyncio.create_task(self._transport.call(tool, arguments))
        self._inflight = task
        try:
            return await asyncio.wait_for(task, timeout)
        except TimeoutError:
            self._interrupted = True
            self._transport.interrupt()
            await self._transport.close()
            raise DriverAbort("Cua 請求逾時；已停止自己的驅動程序，動作結果不明，不能直接重試。") from None
        except asyncio.CancelledError:
            if self._interrupted:
                raise ActionInterrupted("背景動作已中斷；已送出部分需在重新觀察後確認。") from None
            raise
        finally:
            if self._inflight is task:
                self._inflight = None

    async def prepare_for_action(self):
        # Never activates, raises or changes the user's foreground window.
        if self._interrupted or self._closed:
            raise ActionInterrupted("背景工作已暫停或關閉。")

    async def observe(self) -> dict:
        # A failed fresh observation must invalidate all earlier action tokens.
        self._tokens, self._targets = {}, {}
        result = await self._call("get_window_state", {"pid": self.pid, "window_id": self.window_id,
            "session": self._session, "include_screenshot": True, "include_accessibility_tree": True,
            "max_elements": 200, "max_depth": 16, "max_dimension": 1280})
        state, content = _result_parts(result)
        if state.get("pid") != self.pid or state.get("window_id") != self.window_id:
            raise DriverAbort("Cua 回傳的視窗身分不符選定目標。")
        images = [block for block in content if isinstance(block, dict) and block.get("type") == "image"]
        if len(images) != 1 or images[0].get("mimeType") != "image/png" or not state.get("screenshot_frame_valid"):
            raise RuntimeError("Cua 無法提供有明確座標映射的目標視窗截圖。")
        from PIL import Image
        image_data = images[0].get("data", "")
        raw = base64.b64decode(image_data, validate=True)
        with Image.open(io.BytesIO(raw)) as picture:
            width, height = picture.size
        if (width, height) != (state.get("screenshot_width"), state.get("screenshot_height")) or width * height > 16_000_000:
            raise RuntimeError("Cua 截圖尺寸與回傳座標資訊不一致。")
        bounds = state.get("window_bounds", {})
        values = [bounds.get(key) for key in ("x", "y", "width", "height")]
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in values) or values[2] <= 0 or values[3] <= 0:
            raise RuntimeError("Cua 視窗範圍無效。")
        origin_x, origin_y, logical_w, logical_h = values
        scale_x, scale_y = width / logical_w, height / logical_h
        if abs(scale_x - scale_y) > 0.02:
            raise RuntimeError("Cua 視窗截圖比例無法可靠對應座標。")
        self._generation += 1
        self._tokens = {}
        elements = []
        for entry in state.get("elements", [])[:200]:
            if not isinstance(entry, dict) or not isinstance(entry.get("element_token"), str):
                continue
            frame = entry.get("frame") or {}
            coords = [frame.get(key) for key in ("x", "y", "w", "h")]
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in coords):
                continue
            x, y, w, h = coords
            left, top = max(0, (x - origin_x) * scale_x), max(0, (y - origin_y) * scale_y)
            right, bottom = min(width, (x + w - origin_x) * scale_x), min(height, (y + h - origin_y) * scale_y)
            if right <= left or bottom <= top:
                continue
            identifier = f"cua_{self._generation}_{len(elements) + 1}"
            role = str(entry.get("role", "")).removeprefix("AX")
            item = {"id": identifier, "role": role, "text": str(entry.get("label", ""))[:500],
                "x": (left + right) / 2, "y": (top + bottom) / 2, "width": right - left, "height": bottom - top,
                "source": "accessibility", "disabled": entry.get("enabled") is False,
                "actions": entry.get("actions", []), "in_web_content": entry.get("in_web_content", False)}
            if "secure" not in role.lower() and "value" in entry:
                item["value"] = str(entry["value"])[:1200]
            if role.lower() in {"checkbox", "radiobutton"} and isinstance(entry.get("selected"), bool):
                item["checked"] = entry["selected"]
            self._tokens[identifier] = entry
            elements.append(item)
        observation = {"image": "data:image/png;base64," + image_data, "width": width, "height": height,
            "title": str(state.get("window_title") or state.get("app_name") or "Selected window"), "url": "",
            "text": "\n".join(json.dumps(item, ensure_ascii=False) for item in elements), "elements": elements,
            "window": {"pid": self.pid, "window_id": self.window_id}, "input_transport": "cua_background_ax_candidate",
            "agent_cursor_available": False, "physical_input_untouched_verified": False,
            "supported_actions": sorted(self.enabled_actions), "background_input": state.get("background_input", {})}
        observation["key_capabilities"] = {"backend": "cua_background_ax_candidate", "platform": "darwin",
            "supported_keys": sorted(_EDITING_KEYS) if "key" in self.enabled_actions else [],
            "supports_hotkeys": False, "requires_target": True}
        self.set_observation(observation)
        return observation

    async def execute(self, action) -> str:
        await self.prepare_for_action()
        if action.type == "wait":
            await asyncio.sleep(min(max(action.seconds or 1, 0), 10))
            return "等待完成。"
        if action.type == "done":
            return action.text or "模型回報完成；仍需外部驗證。"
        if action.type not in self.enabled_actions:
            raise ValueError("此共用桌面的背景動作尚未通過驗證；不會改用實體滑鼠、切換焦點或全域鍵盤。")
        entry = self._tokens.get(action.target)
        if entry is None or action.x is not None or action.y is not None:
            raise ValueError("背景動作只接受目前視窗的原生元素 ID；不接受像素點擊或未定位鍵盤。")
        role = str(entry.get("role", "")).removeprefix("AX").lower()
        if entry.get("in_web_content") or entry.get("enabled") is False:
            raise ValueError("目前候選背景路徑不支援這個元素。")
        args = {"pid": self.pid, "window_id": self.window_id, "element_token": entry["element_token"],
                "delivery_mode": "background", "session": self._session}
        if action.type == "click":
            if role not in _CLICK_ROLES or "AXPress" not in entry.get("actions", []) or action.button != "left":
                raise ValueError("背景點擊只開放支援 AXPress 的原生按鈕、checkbox 與 radio。")
            tool = "click"
            args.update(button="left", count=1)
        elif action.type == "type":
            if role not in _TEXT_ROLES or len(action.text or "") > 200:
                raise ValueError("背景文字候選路徑只接受原生文字欄位及至多 200 字。")
            tool = "type_text"
            args.update(text=action.text or "", delay_ms=30)
        else:
            if role not in _TEXT_ROLES:
                raise ValueError("背景按鍵需要目前原生文字欄位。")
            keys = _keys(action.key or "", browser=False)
            if len(keys) != 1 or keys[0] not in _EDITING_KEYS:
                raise ValueError("此背景候選路徑尚未開放快捷鍵或可能切換應用程式的按鍵。")
            tool = "press_key"
            args.update(key=keys[0], modifiers=[])
        # Invalidate before dispatch, including a refused/unknown outcome.
        self._tokens = {}
        state, _ = _result_parts(await self._call(tool, args, timeout=12))
        return "已提交背景動作，需重新觀察確認結果：" + json.dumps(state, ensure_ascii=False)[:1500]

    def interrupt_action(self):
        self._interrupted = True
        self._transport.interrupt()
        if self._inflight is not None:
            self._inflight.cancel()

    async def resume_actions(self):
        await self._transport.close()
        self._tokens = {}
        if self._closed:
            raise DriverAbort("背景工作已關閉。")
        self._interrupted = False
        await self._transport.start()

    async def close(self):
        self._closed = True
        self.interrupt_action()
        await self._transport.close()
        self._tokens, self._targets = {}, {}
        self._observation = None
