"""The product requires independent input; never substitute global input."""
from __future__ import annotations

import importlib


def create_desktop_driver(pid=None, window_id=None):
    try:
        module = importlib.import_module("server.cua_driver")
    except ImportError:
        raise RuntimeError("獨立游標與背景鍵盤驅動尚未配置；不會改用會搶走焦點的前景輸入。") from None
    return module.CuaDriver(pid=pid, window_id=window_id)


async def desktop_status() -> dict:
    try:
        module = importlib.import_module("server.cua_driver")
        status = await module.capabilities()
        if (status.get("available") and status.get("physical_input_untouched") is True
                and status.get("agent_cursor_available") is True):
            return status
        return {**status, "available": False}
    except ImportError:
        return {"available": False, "backend": "cua", "physical_input_untouched": False,
                "reason": "獨立游標與背景鍵盤驅動尚未配置；桌面操作目前停用，避免搶走你的滑鼠或鍵盤焦點。"}


async def desktop_windows() -> dict:
    """Explicit UI discovery; health checks never launch a native process."""
    status = await desktop_status()
    if not status.get("installed"):
        return {"windows": [], "available": False, "error": status.get("reason", "背景驅動未安裝")}
    try:
        module = importlib.import_module("server.cua_driver")
        windows = await module.list_windows()
        return {"windows": windows, "available": status["available"]}
    except Exception:
        return {"windows": [], "available": False,
                "error": "無法讀取背景視窗清單；請查看背景驅動診斷與 macOS 權限狀態。"}
