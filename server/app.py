from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from .agent import ACTIVE, Run
from .background import desktop_status, desktop_windows
from .drivers import capabilities
from .perception import perception_capabilities
from .preview import BrowserPreview
from .providers import DEFAULT_URLS, test_connection
from .schemas import Approval, ModelConfig, RunRequest, UserInput

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("COMPUTERUSE_PORT", "8765"))
config = ModelConfig()
runs: dict[str, Run] = {}
run_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app):
    yield
    await asyncio.gather(*(run.stop() for run in runs.values() if run.task and not run.task.done()), return_exceptions=True)


app = FastAPI(title="ComputerUSE", version="0.1.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])


@app.middleware("http")
async def local_boundary(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin:
            try:
                parts = urlsplit(origin)
                valid = parts.scheme in ("http", "https") and parts.hostname in ("127.0.0.1", "localhost", "::1") and parts.port in (PORT, 5173) and not parts.username and not parts.password
            except ValueError:
                valid = False
            if not valid:
                return JSONResponse({"detail": "只接受本機工作台的請求"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "拒絕跨網站存取本機控制介面"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-computeruse") != "1" or request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "缺少本機控制請求標記或 JSON Content-Type"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default response echoes submitted values, including credentials.
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


def public_config(value: ModelConfig):
    return {**value.model_dump(exclude={"api_key"}), "has_key": bool(value.api_key)}


def with_saved_key(value: ModelConfig):
    value = value.model_copy(deep=True)
    old_url = config.base_url or DEFAULT_URLS.get(config.provider, "")
    new_url = value.base_url or DEFAULT_URLS.get(value.provider, "")
    if not value.api_key and value.provider == config.provider and old_url == new_url:
        value.api_key = config.api_key
    return value


@app.get("/api/health")
async def health():
    driver_caps, perception, background = await asyncio.gather(capabilities(), perception_capabilities(), desktop_status())
    return {"ok": True, "version": "0.1.0", "capabilities": {**driver_caps, "desktop": background["available"],
        "desktop_hint": background.get("reason", ""), "desktop_backend": background, "perception": perception}}


@app.get("/api/config")
async def get_config():
    return public_config(config)


@app.post("/api/config")
async def set_config(value: ModelConfig):
    global config
    config = with_saved_key(value)
    return public_config(config)


@app.post("/api/config/test")
async def check_connection(value: ModelConfig):
    return await test_connection(with_saved_key(value))


@app.delete("/api/config/key")
async def clear_key():
    config.api_key = ""
    return public_config(config)


@app.get("/api/models")
async def local_models():
    # Discovery only: does not load/download/run a model or inspect credentials.
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            response = await client.get("http://127.0.0.1:11434/api/tags")
            response.raise_for_status()
            models = response.json().get("models", [])
        return {"models": [{k: m.get(k) for k in ("name", "size", "capabilities", "details")} for m in models]}
    except (httpx.HTTPError, ValueError):
        return {"models": [], "error": "本機 Ollama 未回應"}


@app.get("/api/sessions")
async def list_sessions():
    return [run.snapshot() for run in reversed(list(runs.values()))]


@app.get("/api/desktop/windows")
async def list_desktop_windows():
    return await desktop_windows()


def find_run(run_id: str) -> Run:
    if run_id not in runs:
        raise HTTPException(404, "找不到此任務；重啟服務後記憶體內紀錄會清除。")
    return runs[run_id]


@app.post("/api/sessions")
async def start_run(request: RunRequest):
    async with run_lock:
        if any(run.task and not run.task.done() for run in runs.values()):
            raise HTTPException(409, "已有任務執行中；請先停止或完成目前任務。")
        if config.provider == "demo" and request.target != "browser":
            raise HTTPException(400, "本機自我檢查只使用隔離瀏覽器。桌面任務請先選擇模型。")
        if config.provider in ("openai", "anthropic", "gemini") and not config.api_key:
            raise HTTPException(400, "請先在模型設定中填入 API 金鑰。")
        if request.target == "desktop":
            caps = await desktop_status()
            if not caps["available"]:
                raise HTTPException(400, caps.get("reason") or "獨立背景輸入目前不可用")
            if request.desktop_pid is None or request.desktop_window_id is None:
                raise HTTPException(400, "請先選擇代理專用的背景視窗；不會自動接管前景視窗。")
        run = Run(request, config, f"http://127.0.0.1:{PORT}/demo")
        runs[run.id] = run
        # Bound memory use while keeping recent exported-ready sessions.
        while len(runs) > 40:
            oldest = next(iter(runs))
            del runs[oldest]
        run.task = asyncio.create_task(run.execute_loop(), name=f"computeruse-{run.id}")
        return run.snapshot()


@app.get("/api/sessions/{run_id}")
async def get_run(run_id: str):
    return find_run(run_id).snapshot()


@app.post("/api/sessions/{run_id}/stop")
async def stop_run(run_id: str):
    run = find_run(run_id)
    await run.stop()
    return run.snapshot()


@app.post("/api/sessions/{run_id}/approve")
async def approve_run(run_id: str, value: Approval):
    run = find_run(run_id)
    if not run.pending_action or run.approval.is_set():
        raise HTTPException(409, "目前沒有等待確認的操作")
    run.approved = value.approved
    run.approval.set()
    return run.snapshot()


@app.post("/api/sessions/{run_id}/pause")
async def pause_run(run_id: str):
    run = find_run(run_id)
    if run.status not in ACTIVE:
        raise HTTPException(409, "此任務已結束")
    run.paused = True
    if run.driver and hasattr(run.driver, "interrupt_action"):
        run.driver.interrupt_action()
    run.revision += 1
    run.resume.clear()
    run.status = "paused"
    run.emit("info", "已要求暫停；目前進行中的操作結束後將不再執行新動作。")
    return run.snapshot()


@app.post("/api/sessions/{run_id}/resume")
async def resume_run(run_id: str):
    run = find_run(run_id)
    if not run.paused:
        raise HTTPException(409, "任務沒有暫停")
    revision = run.revision
    if run.driver and hasattr(run.driver, "resume_actions"):
        await run.driver.resume_actions()
    if run.stop_requested or run.status not in ACTIVE:
        raise HTTPException(409, "任務已停止")
    if revision != run.revision:
        if run.driver and hasattr(run.driver, "interrupt_action"):
            run.driver.interrupt_action()
        raise HTTPException(409, "任務狀態已更新，請重新確認後繼續")
    run.paused = False
    run.status = "awaiting_approval" if run.pending_action else "awaiting_input" if run.pending_input else "running"
    run.resume.set()
    run.emit("info", "已繼續任務。")
    return run.snapshot()


@app.post("/api/sessions/{run_id}/input")
async def input_run(run_id: str, value: UserInput):
    run = find_run(run_id)
    if run.status not in ACTIVE:
        raise HTTPException(409, "此任務已結束；請建立新任務。")
    run.interventions.append(value.text)
    if run.driver and hasattr(run.driver, "interrupt_action"):
        run.driver.interrupt_action()
    run.revision += 1
    run.input_ready.set()
    run.emit("info", "使用者補充：" + value.text)
    # Invalidate a pending action on steering. Reject that action without ending.
    if run.pending_action:
        run.approved = False
        run.approval.set()
    return run.snapshot()


@app.get("/api/sessions/{run_id}/screenshot")
async def screenshot(run_id: str):
    shot = find_run(run_id).screenshot()
    if not shot:
        raise HTTPException(404, "尚未取得畫面")
    return Response(shot[0], media_type=shot[1])


@app.get("/api/sessions/{run_id}/view")
async def live_view(run_id: str, request: Request):
    run = find_run(run_id)
    if run.request.target != "browser":
        raise HTTPException(400, "即時預覽目前只提供代理專用瀏覽器。")
    if not hasattr(run, "preview"):
        run.preview = BrowserPreview(run)
    return StreamingResponse(run.preview.stream(request.is_disconnected),
        media_type="multipart/x-mixed-replace; boundary=computeruse-frame",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/sessions/{run_id}/export")
async def export_run(run_id: str):
    value = find_run(run_id).snapshot()
    return Response(json.dumps(value, ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="computeruse-{run_id}.json"'})


@app.get("/api/sessions/{run_id}/files/{file_id}")
async def download_file(run_id: str, file_id: str):
    run = find_run(run_id)
    path = run.download_paths.get(file_id)
    if path is None or not path.is_file() or path.is_symlink():
        raise HTTPException(404, "找不到此任務下載的檔案")
    resolved = path.resolve()
    if not resolved.is_relative_to(run.download_dir.resolve()):
        raise HTTPException(403, "檔案不在任務輸出目錄")
    entry = next((f for f in run.downloads if f.get("id") == file_id), {})
    return FileResponse(resolved, filename=entry.get("name", resolved.name), media_type="application/octet-stream")


@app.get("/demo", response_class=HTMLResponse)
async def demo():
    return DEMO_HTML


DEMO_HTML = '''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>ComputerUSE 本機測試頁</title>
<style>body{margin:0;background:#f5f3ed;color:#28271f;font:18px system-ui}main{max-width:700px;margin:100px auto;padding:42px;background:white;border:1px solid #e0ddd3;border-radius:20px}small{color:#c05b34;letter-spacing:2px}h1{font-size:36px}p{line-height:1.7;color:#767367}label{display:block;margin-bottom:12px}input{padding:15px;font:inherit;border:1px solid #d6d1c5;border-radius:8px;width:60%}button{margin-left:12px;padding:16px 24px;background:#ca5d39;color:white;border:0;border-radius:8px;font:inherit;cursor:pointer}#result{margin-top:26px;padding:20px;background:#e9f0e6;border-radius:10px}</style>
<main><small>COMPUTERUSE · LOCAL WORKSPACE</small><h1>讓模型，開始動手。</h1><p>這是本機的操作測試環境。輸入名稱，再完成測試。<br>所有表單資料只存在這個頁面。</p>
<form id="form"><label for="name">你的名稱</label><input id="name" placeholder="輸入 ComputerUSE" autocomplete="off"><button type="submit">完成測試</button></form><div id="result" hidden></div></main>
<script>document.getElementById('form').onsubmit=e=>{e.preventDefault();const name=document.getElementById('name').value;const result=document.getElementById('result');result.hidden=false;result.textContent=name==='ComputerUSE'?'測試任務已完成：歡迎，ComputerUSE。':'請輸入 ComputerUSE 後重試。';};</script></html>'''


DIST = ROOT / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/", response_class=HTMLResponse)
async def index():
    if (DIST / "index.html").exists():
        return FileResponse(DIST / "index.html")
    return HTMLResponse("<h1>ComputerUSE</h1><p>請先執行 ./start.sh 建立前端，或 cd frontend && npm run dev</p>")
