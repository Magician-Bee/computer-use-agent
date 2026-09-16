"""Isolated API fixture for frontend tests; no model or desktop dependencies.

Serves frontend/dist and a deliberately small, stateful API on an ephemeral
loopback port. The /_test routes exist only in this test process.
"""
from __future__ import annotations

import copy
import base64
import json
import mimetypes
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

DIST = Path(__file__).resolve().parents[1] / "dist"
CONFIG = {
    "provider": "custom", "base_url": "https://mock.invalid/v1",
    "model": "mock-planner", "vision": False, "perception": "auto",
    "max_tokens": 2048, "ocr_engine": "auto", "ocr_model": "glm-ocr:latest",
    "ocr_base_url": "http://127.0.0.1:11434", "has_key": True,
}
HEALTH = {
    "ok": True, "version": "contract-test",
    "capabilities": {
        "browser": True, "desktop": True, "desktop_hint": "Test fixture only",
        "perception": {"ocr": True, "ocr_engine": "mock-ocr", "yolo": False},
    },
}
CONFIG_REQUEST_FIELDS = (set(CONFIG) - {"has_key"}) | {"api_key"}
# A synthetic 1 px image validates image transport only, never cursor motion.
IMAGE = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jA1sAAAAASUVORK5CYII=")
STATE = {
    "config": copy.deepcopy(CONFIG), "sessions": [], "requests": [],
    "test_error": False, "poll_count": 0, "missing": False,
    "download_missing": False,
    "desktop_mode": "unavailable", "window_reads": 0,
    "preview_reads": [], "preview_error": False,
}
LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(run, message):
    run["updated_at"] = now()
    run["events"].append({
        "id": len(run["events"]) + 1, "at": run["updated_at"],
        "kind": "info", "message": message,
    })


def make_session(data):
    return {
        "id": "contract-run", "task": data["task"], "target": data["target"],
        "approval_mode": data["approval_mode"], "max_steps": data["max_steps"],
        "browser_visible": data["browser_visible"], "status": "running",
        "desktop_pid": data.get("desktop_pid"), "desktop_window_id": data.get("desktop_window_id"),
        "created_at": now(), "updated_at": now(), "step": 1,
        "model": STATE["config"]["model"], "provider": STATE["config"]["provider"],
        "events": [], "pending_action": None, "pending_input": None,
        "screenshot_available": False, "observation": None, "error": None,
        "downloads": [],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send(self, status, body, content_type="application/json", headers=None):
        raw = json.dumps(body, ensure_ascii=False).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass  # React may abort an in-flight poll during a state transition.

    def read_json(self):
        return json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")

    def do_GET(self):
        with LOCK:
            self.get()

    def get(self):
        path = urlsplit(self.path).path
        if path == "/api/desktop/windows":
            STATE["window_reads"] += 1
            if STATE["desktop_mode"] == "error":
                return self.send(503, {"detail": "模擬視窗清單讀取失敗"})
            return self.send(200, {
                "available": STATE["desktop_mode"] == "ready",
                "error": "背景輸入尚未通過互不干擾驗證" if STATE["desktop_mode"] == "unavailable" else None,
                "windows": [] if STATE["desktop_mode"] == "empty" else [
                    {"pid": 410, "window_id": 710, "app_name": "Fixture Editor", "title": "同名文件", "is_on_screen": True},
                    {"pid": 411, "window_id": 711, "app_name": "Fixture Editor", "title": "同名文件", "is_on_screen": False},
                ],
            })
        fixed = {
            "/_test/state": STATE, "/api/health": {**HEALTH, "capabilities": {
                **HEALTH["capabilities"], "desktop": STATE["desktop_mode"] == "ready",
                "desktop_backend": {"backend": "mock-cua", "version": "test-only", "installed": True,
                    "available": STATE["desktop_mode"] == "ready",
                    "physical_input_untouched": STATE["desktop_mode"] == "ready",
                    "agent_cursor_available": STATE["desktop_mode"] == "ready",
                    "reason": "背景輸入尚未通過互不干擾驗證"},
            }},
            "/api/config": STATE["config"], "/api/sessions": STATE["sessions"],
            "/api/models": {"models": [{"name": name} for name in (
                "minicpm-v4.6:latest", "qwen3-vl:2b", "glm-ocr:latest",
            )]},
        }
        if path in fixed:
            return self.send(200, fixed[path])
        if path == "/api/sessions/contract-run":
            STATE["poll_count"] += 1
            if STATE["missing"]:
                return self.send(404, {"detail": "找不到此任務；測試模擬服務重啟。"})
            return self.send(200, STATE["sessions"][0])
        if path == "/api/sessions/contract-run/export":
            return self.send(200, STATE["sessions"][0])
        if path in ("/api/sessions/contract-run/view", "/api/sessions/contract-run/screenshot"):
            STATE["preview_reads"].append(path)
            if path.endswith("/view"):
                if STATE["preview_error"]:
                    return self.send(503, {"detail": "Synthetic stream interruption"})
                frame = (b"--computeruse-frame\r\nContent-Type: image/png\r\nContent-Length: "
                         + str(len(IMAGE)).encode() + b"\r\n\r\n" + IMAGE + b"\r\n")
                return self.send(200, frame + b"--computeruse-frame--\r\n",
                                 "multipart/x-mixed-replace; boundary=computeruse-frame")
            return self.send(200, IMAGE, "image/png")
        if path == "/api/sessions/contract-run/files/report":
            if STATE["download_missing"]:
                return self.send(404, {"detail": "測試檔案已不存在"})
            return self.send(200, b"contract download fixture\n", "application/octet-stream",
                             {"Content-Disposition": 'attachment; filename="report.txt"'})
        if path.startswith("/api/"):
            return self.send(404, {"detail": "Unknown mock endpoint"})
        file = (DIST / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if not file.is_relative_to(DIST) or not file.is_file():
            return self.send(404, {"detail": "Not found"})
        return self.send(200, file.read_bytes(), mimetypes.guess_type(str(file))[0] or "application/octet-stream")

    def do_POST(self):
        with LOCK:
            self.mutate("POST")

    def do_DELETE(self):
        with LOCK:
            self.mutate("DELETE")

    def mutate(self, method):
        data = self.read_json()
        if self.path == "/_test/scenario":
            return self.scenario(data["name"])
        if (self.headers.get("X-ComputerUse") != "1"
                or self.headers.get("Content-Type", "").split(";")[0] != "application/json"):
            return self.send(403, {"detail": "Missing frontend control request headers"})
        STATE["requests"].append({
            "method": method, "path": self.path,
            "body": {key: value for key, value in data.items() if key != "api_key"},
            "key_present": bool(data.get("api_key")),
            "header": self.headers.get("X-ComputerUse"),
        })
        if method == "DELETE" and self.path == "/api/config/key":
            STATE["config"]["has_key"] = False
            return self.send(200, STATE["config"])
        if self.path in ("/api/config", "/api/config/test"):
            if set(data) - CONFIG_REQUEST_FIELDS:
                return self.send(422, {"detail": [{"msg": "Extra inputs are not permitted"}]})
            if self.path.endswith("/test"):
                return self.send(200, {
                    "ok": not STATE["test_error"],
                    "message": "模擬測試錯誤：請修正端點" if STATE["test_error"] else "模擬連線通過；未執行模型",
                })
            previous = STATE["config"]
            keep_key = (previous["provider"] == data["provider"]
                        and previous["base_url"] == data["base_url"]
                        and previous["has_key"])
            STATE["config"] = {key: value for key, value in data.items() if key != "api_key"}
            STATE["config"]["has_key"] = bool(data.get("api_key")) or keep_key
            return self.send(200, STATE["config"])
        if self.path == "/api/sessions":
            run = make_session(data)
            STATE["sessions"] = [run]
            STATE["missing"] = False
            return self.send(200, run)
        if self.path.startswith("/api/sessions/contract-run/"):
            run = STATE["sessions"][0]
            operation = self.path.rsplit("/", 1)[1]
            if operation == "pause":
                run["status"] = "paused"
                emit(run, "已暫停")
            elif operation == "resume":
                run["status"] = ("awaiting_approval" if run["pending_action"]
                                 else "awaiting_input" if run["pending_input"] else "running")
                emit(run, "已繼續")
            elif operation == "input":
                run["pending_input"] = None
                if run["status"] == "awaiting_input":
                    run["status"] = "running"
                emit(run, "使用者補充：" + data["text"])
            elif operation == "stop":
                run.update(status="stopped", pending_action=None, pending_input=None)
                emit(run, "已停止")
            elif operation != "approve":
                return self.send(404, {"detail": "Unknown mock operation"})
            # Approval intentionally returns the same pending state to exercise
            # the real API's delay between acknowledgment and agent consumption.
            return self.send(200, run)
        return self.send(404, {"detail": "Unknown mock endpoint"})

    def scenario(self, name):
        run = STATE["sessions"][0] if STATE["sessions"] else None
        if name == "error":
            run.update(status="failed", error="僅有 session.error 的測試錯誤", events=[], updated_at=now())
        elif name == "input":
            run.update(status="awaiting_input", pending_input="請在獨立瀏覽器完成登入")
            emit(run, "等待使用者回覆")
        elif name == "preview":
            run.update(screenshot_available=True, observation={
                "width": 1, "height": 1, "url": "https://mock.invalid/fixture",
                "title": "Synthetic preview", "text": "固定的決策觀察文字", "elements": [],
            })
            emit(run, "模擬畫面就緒")
        elif name in ("preview_error", "preview_recovered"):
            STATE["preview_error"] = name == "preview_error"
        elif name == "approval":
            run.update(status="awaiting_approval", pending_action={"type": "click", "target": "e1", "reason": "測試確認"})
            emit(run, "等待確認")
        elif name == "completed":
            run.update(status="completed", pending_action=None, pending_input=None, updated_at=now())
            run["events"].append({"id": 99, "at": now(), "kind": "done", "message": "模擬完成結果：契約流程已檢查。"})
            run["downloads"] = [{"id": "report", "name": "report.txt", "size": 26}]
        elif name == "missing":
            STATE.update(missing=True, sessions=[])
            STATE["config"] = dict(CONFIG, provider="demo", model="local-demo", base_url="", has_key=False)
        elif name == "download_missing":
            STATE["download_missing"] = True
        elif name == "test_error":
            STATE["test_error"] = True
        elif name.startswith("desktop_"):
            STATE["desktop_mode"] = name.removeprefix("desktop_")
            STATE["config"] = dict(CONFIG, has_key=False)
        else:
            return self.send(400, {"detail": "Unknown test scenario"})
        return self.send(200, {"ok": True})


if __name__ == "__main__":
    if not (DIST / "index.html").is_file():
        raise SystemExit("Missing frontend/dist. Run npm run build before starting UI tests.")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    print(json.dumps({"origin": f"http://127.0.0.1:{server.server_port}"}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
