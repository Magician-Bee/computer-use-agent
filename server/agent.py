"""One active task; cancellable planning, bounded recovery, human intervention."""
from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timezone
import uuid
from pathlib import Path

from .drivers import BrowserDriver
from .background import create_desktop_driver as DesktopDriver
from .grounding import StaleObservation, remap_approved_action
from .feedback import visible_effect, action_fingerprint
from .perception import enrich_observation
from .providers import ProviderError, next_action
from .schemas import Action, ModelConfig, RunRequest

ACTIVE = {"starting", "running", "awaiting_approval", "paused", "awaiting_input"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(action: Action) -> dict:
    return action.model_dump(exclude_none=True, exclude_defaults=True)


class Run:
    def __init__(self, request: RunRequest, config: ModelConfig, demo_url: str):
        self.request = request
        self.config = config.model_copy(deep=True)
        self.demo_url = demo_url
        self.id = uuid.uuid4().hex[:16]
        self.status = "starting"
        self.created_at = self.updated_at = now()
        self.step = 0
        self.events: list[dict] = []
        self.history: list[dict] = []
        self.observation: dict | None = None
        self.pending_action: Action | None = None
        self.pending_input: str | None = None
        self.error: str | None = None
        self.task: asyncio.Task | None = None
        self.driver = None
        self.approval = asyncio.Event()
        self.approved = False
        self.resume = asyncio.Event()
        self.resume.set()
        self.input_ready = asyncio.Event()
        self.paused = False
        self.stop_requested = False
        self.interventions: list[str] = []
        self.user_instructions: list[str] = []
        self.revision = 0
        self.download_dir = Path(__file__).resolve().parent.parent / "artifacts" / "downloads" / self.id
        self.downloads: list[dict] = []
        self.download_paths: dict = {}

    def emit(self, kind: str, message: str, action: Action | None = None):
        self.updated_at = now()
        item = {"id": len(self.events) + 1, "at": self.updated_at, "kind": kind, "message": message}
        if action is not None:
            item["action"] = compact(action)
        self.events.append(item)

    def snapshot(self) -> dict:
        obs = self.observation
        return {
            "id": self.id, "task": self.request.task, "target": self.request.target,
            "approval_mode": self.request.approval_mode, "max_steps": self.request.max_steps,
            "browser_visible": self.request.browser_visible if self.request.target == "browser" else False,
            "desktop_pid": self.request.desktop_pid,
            "desktop_window_id": self.request.desktop_window_id,
            "status": self.status, "created_at": self.created_at, "updated_at": self.updated_at,
            "step": self.step, "model": self.config.model, "provider": self.config.provider,
            "events": self.events, "pending_action": compact(self.pending_action) if self.pending_action else None,
            "pending_input": self.pending_input, "screenshot_available": bool(obs and obs.get("image")),
            "observation": {k: obs.get(k) for k in ("width", "height", "url", "title", "text", "elements", "perception",
                "cursor", "input_transport", "physical_input_untouched", "agent_cursor_available")} if obs else None,
            "error": self.error,
            "downloads": self.downloads,
        }

    async def checkpoint(self):
        if self.stop_requested:
            raise asyncio.CancelledError
        if self.paused:
            self.status = "paused"
            await self.resume.wait()
        if self.stop_requested:
            raise asyncio.CancelledError
        if self.status not in ("awaiting_approval", "awaiting_input"):
            self.status = "running"

    async def observe(self, *, quick: bool = False) -> dict:
        obs = await self.driver.observe()
        obs["target"] = self.request.target
        obs["_grounding_text"] = obs.get("text", "")
        if self.config.perception in ("ocr", "ocr_yolo") or self.request.target == "desktop" or self.config.ocr_engine == "glm_ocr":
            obs = await enrich_observation(obs, use_yolo=self.config.perception == "ocr_yolo",
                ocr_engine="native" if quick else self.config.ocr_engine, ocr_model=self.config.ocr_model,
                ocr_base_url=self.config.ocr_base_url)
        else:
            obs["perception"] = {"engine": "DOM", "ocr": False, "yolo": False}
        self.driver.set_observation(obs)
        self.observation = obs
        self.downloads = getattr(self.driver, "downloads", [])
        self.download_paths = getattr(self.driver, "download_paths", {})
        self.updated_at = now()
        warning = obs.get("perception", {}).get("warning")
        if warning and not any(event["message"] == warning for event in self.events):
            self.emit("info", warning)
        return obs

    def demo_action(self, obs: dict) -> Action:
        # A deterministic, visible integration self-check, explicitly NOT AI inference.
        text = obs.get("text", "")
        if "測試任務已完成" in text:
            return Action(type="done", text="已在本機測試頁輸入 ComputerUSE，點擊完成按鈕，並讀到成功訊息。這是操作流程自我檢查，未呼叫 AI 模型。")
        elements = obs.get("elements", [])
        if not any(h.get("action", {}).get("type") == "type" for h in self.history):
            el = next((e for e in elements if e.get("role") in ("textbox", "input") or e.get("tag") in ("input", "textarea")), None)
            if el:
                return Action(type="type", target=el["id"], text="ComputerUSE", reason="在測試頁輸入名稱", expected="輸入欄顯示 ComputerUSE")
        button = next((e for e in elements if "完成測試" in e.get("text", "")), None)
        if button:
            return Action(type="click", target=button["id"], reason="送出本機測試表單", expected="顯示測試任務已完成")
        raise RuntimeError("本機示範頁的元素未就緒，請重新啟動示範。")

    async def execute_loop(self):
        candidate_done = False
        errors = 0
        repeated = 0
        stale_count = 0
        previous = None
        pending_effect = None
        try:
            start_url = self.demo_url if self.config.provider == "demo" else self.request.start_url
            self.driver = BrowserDriver(start_url=start_url) if self.request.target == "browser" else DesktopDriver(
                pid=self.request.desktop_pid, window_id=self.request.desktop_window_id)
            if self.request.target == "browser":
                self.driver.headless = not self.request.browser_visible
            if hasattr(self.driver, "configure_files"):
                self.driver.configure_files(self.request.allowed_uploads, self.download_dir)
            await self.driver.start()
            self.emit("info", "已啟動隔離瀏覽器。" if self.request.target == "browser" else "已連接獨立的背景桌面輸入通道。")
            if self.config.provider == "demo":
                self.emit("info", "正在執行固定的本機自我檢查，不使用 AI 模型。")
            elif not self.config.vision:
                self.emit("info", "純文字模型模式：只傳送文字、元素編號與座標，不傳送截圖。")
            while self.step < self.request.max_steps:
                await self.checkpoint()
                if hasattr(self.driver, "resume_actions"):
                    await self.driver.resume_actions()
                    await self.checkpoint()
                observation_revision = self.revision
                try:
                    obs = await self.observe()
                except Exception as exc:
                    if getattr(exc, "interrupted", False):
                        self.emit("info", "畫面擷取已中斷；繼續時將重新觀察。")
                        continue
                    raise
                if pending_effect:
                    baseline, previous_action, entry = pending_effect
                    entry["result"] += "; " + visible_effect(baseline, obs, previous_action)
                    pending_effect = None
                self.emit("observation", f"已觀察畫面：{obs.get('title') or '桌面'} · {len(obs.get('elements', []))} 個元素")
                if self.interventions:
                    additions, self.interventions = self.interventions, []
                    for text in additions:
                        self.user_instructions.append(text)
                        self.history.append({"action": {"type": "user_input"}, "result": text})
                await self.checkpoint()
                if observation_revision != self.revision:
                    self.emit("info", "暫停或補充指令期間狀態已變更，重新擷取畫面。")
                    continue
                decision_revision = self.revision
                try:
                    action = self.demo_action(obs) if self.config.provider == "demo" else await next_action(
                        self.config, self.request.task + ("\n\nUser follow-up instructions:\n" + "\n".join(self.user_instructions) if self.user_instructions else ""), obs, self.history
                    )
                except ProviderError as exc:
                    errors += 1
                    self.emit("error", str(exc))
                    if errors >= 3:
                        raise RuntimeError("模型連續三次無法提供可用操作；已停止，請調整設定後重試。") from None
                    self.history.append({"action": {"type": "model_error"}, "result": str(exc)})
                    await asyncio.sleep(1)
                    continue
                await self.checkpoint()
                # Steering received during model inference invalidates its decision.
                if self.interventions or self.revision != decision_revision:
                    self.emit("info", "已收到補充指令，重新規劃下一步。")
                    continue
                if action.type == "ask_user":
                    self.pending_input = action.text
                    self.input_ready.clear()
                    self.status = "awaiting_input"
                    self.emit("info", action.text or "需要補充資訊")
                    await self.input_ready.wait()
                    self.pending_input = None
                    self.status = "running"
                    candidate_done = False
                    continue
                if action.type == "done":
                    if not candidate_done and self.config.provider != "demo":
                        candidate_done = True
                        self.emit("info", "模型回報完成，正在重新觀察畫面並確認結果。")
                        self.history.append({"action": compact(action), "result": "Completion proposed, NOT yet accepted. A fresh observation follows. Verify the USER_TASK's result against visible evidence; return done only if achieved, otherwise continue or ask_user."})
                        continue
                    self.status = "completed"
                    self.emit("done", action.text or "任務完成")
                    return
                candidate_done = False
                if self.request.approval_mode == "always" and action.type != "wait":
                    self.pending_action = action
                    self.approved = False
                    self.approval.clear()
                    self.status = "awaiting_approval"
                    self.emit("info", "等待你確認下一個操作。", action)
                    await self.approval.wait()
                    self.pending_action = None
                    if not self.approved:
                        if self.interventions:
                            self.status = "running"
                            self.emit("info", "已取消原操作，依補充指令重新規劃。")
                            continue
                        self.status = "stopped"
                        self.emit("info", "你已拒絕此操作，任務已停止。")
                        return
                    self.status = "running"
                    await self.checkpoint()
                    if self.interventions or self.revision != decision_revision:
                        self.emit("info", "任務狀態已變更，重新觀察後規劃操作。")
                        continue
                # Even autopilot inference can take long enough for a page or
                # foreground app to change. Revalidate every action, using fast
                # native OCR rather than another GLM/VLM inference request.
                try:
                    if hasattr(self.driver, "prepare_for_action"):
                        await self.driver.prepare_for_action()
                    fresh = await self.observe(quick=True)
                except Exception as exc:
                    if getattr(exc, "interrupted", False):
                        self.emit("info", "操作準備已中斷；繼續時將重新觀察。")
                        continue
                    raise
                try:
                    action = remap_approved_action(action, obs, fresh)
                except StaleObservation as exc:
                    stale_count += 1
                    self.emit("info", str(exc) + "；重新規劃下一步。")
                    self.history.append({"action": compact(action), "result": "NOT EXECUTED: " + str(exc) + "; use only element IDs from the next fresh observation, never IDs from action history."})
                    if stale_count >= 5:
                        raise RuntimeError("畫面持續變動，連續五次無法確認操作目標。請讓目標視窗穩定後重試。") from None
                    continue
                stale_count = 0
                await self.checkpoint()
                if self.interventions or self.revision != decision_revision:
                    continue
                fingerprint = action_fingerprint(action, fresh)
                repeated = repeated + 1 if fingerprint == previous else 0
                previous = fingerprint
                if repeated >= 3:
                    raise RuntimeError("相同畫面上重複相同操作四次；已停止以避免無限循環。請補充更明確的任務後重試。")
                self.step += 1
                self.emit("action", action.reason or f"執行 {action.type}", action)
                try:
                    result = await self.driver.execute(action)
                    errors = 0
                    entry = {"action": compact(action), "result": result + (f"; expected visible outcome: {action.expected}" if action.expected else "")}
                    self.history.append(entry)
                    pending_effect = (fresh, action, entry)
                    self.emit("result", result)
                except Exception as exc:
                    if getattr(exc, "interrupted", False):
                        self.history.append({"action": compact(action), "result": "INTERRUPTED: input paused or task changed. Partial effects may exist; reobserve before deciding the next action. Do not assume rollback or repeat a submission."})
                        self.emit("info", "動作已中斷；將重新觀察確認已發生的變更。")
                        continue
                    if getattr(exc, "fatal", False) or type(exc).__name__ == "FailSafeException" or "緊急停止" in str(exc):
                        self.status = "stopped"
                        message = "已觸發滑鼠緊急停止；不再執行後續操作。" if type(exc).__name__ == "FailSafeException" else "操作已停止：" + (str(exc)[:1500] or type(exc).__name__)
                        self.emit("info", message)
                        return
                    errors += 1
                    # Driver errors are locally controlled and carry useful permission guidance.
                    message = str(exc)[:1500] or type(exc).__name__
                    self.history.append({"action": compact(action), "result": "FAILED: " + message + "; re-observe and choose a corrected action"})
                    self.emit("error", "操作失敗，將重新觀察：" + message)
                    if errors >= 3:
                        raise RuntimeError("連續三次操作失敗：" + message) from None
                await asyncio.sleep(0.35)
            # Capture final result before closing the browser, even at the step limit.
            await self.observe()
            self.status = "limit_reached"
            self.emit("info", f"已達 {self.request.max_steps} 步上限；目前畫面與紀錄已保留。")
        except asyncio.CancelledError:
            self.status = "stopped"
            self.emit("info", "任務已停止，正在釋放操作資源。")
        except Exception as exc:
            self.status = "failed"
            self.error = str(exc)[:1800] or type(exc).__name__
            self.emit("error", self.error)
        finally:
            self.pending_action = None
            self.pending_input = None
            if self.driver:
                with contextlib.suppress(Exception):
                    await self.driver.close()
                self.downloads = getattr(self.driver, "downloads", [])
                self.download_paths = getattr(self.driver, "download_paths", {})
            # Credentials are held only for the run lifetime, never exported.
            self.config.api_key = ""
            self.updated_at = now()

    async def stop(self):
        already_stopping = self.stop_requested
        self.stop_requested = True
        if self.driver and hasattr(self.driver, "interrupt_action"):
            self.driver.interrupt_action()
        self.resume.set()
        self.approval.set()
        self.input_ready.set()
        if self.task and not self.task.done():
            if not already_stopping:
                self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(self.task)
        if self.status in ACTIVE:
            self.status = "stopped"
        self.config.api_key = ""

    def screenshot(self) -> tuple[bytes, str] | None:
        value = (self.observation or {}).get("image", "")
        if not value.startswith("data:image/"):
            return None
        prefix, encoded = value.split(",", 1)
        return base64.b64decode(encoded), prefix[5:].split(";")[0]
