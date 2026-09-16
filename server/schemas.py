from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["openai", "anthropic", "gemini", "ollama", "custom", "demo"] = "demo"
    base_url: str = ""
    model: str = "local-demo"
    api_key: str = Field(default="", repr=False, max_length=4096)
    vision: bool = False
    perception: Literal["auto", "ocr", "ocr_yolo"] = "auto"
    ocr_engine: Literal["auto", "native", "glm_ocr"] = "auto"
    ocr_model: str = Field(default="glm-ocr:latest", min_length=1, max_length=200)
    ocr_base_url: str = "http://127.0.0.1:11434"
    max_tokens: int = Field(default=2048, ge=256, le=16384)

    @field_validator("model", "base_url", "api_key")
    @classmethod
    def trim(cls, value: str) -> str:
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value)
        _ = parsed.port
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("API 網址必須使用 http 或 https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API 網址不可包含帳密、查詢參數或片段")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("遠端 API 請使用 HTTPS；HTTP 僅適用本機模型")
        return value.rstrip("/")

    @field_validator("ocr_base_url")
    @classmethod
    def local_ocr(cls, value: str) -> str:
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or p.hostname not in ("127.0.0.1", "localhost", "::1") or p.username or p.password or p.query or p.fragment:
            raise ValueError("OCR 端點必須是本機 HTTP(S) Ollama 網址")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_model(self):
        if not self.model:
            raise ValueError("請填入模型名稱")
        if self.provider == "custom" and not self.base_url:
            raise ValueError("自訂供應商需要 API Base URL")
        return self


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["navigate", "click", "double_click", "type", "key", "scroll", "wait", "done", "move", "drag", "open_app", "back", "forward", "ask_user", "select_option", "new_tab", "switch_tab", "close_tab", "upload_file"]
    x: float | None = Field(default=None, ge=0, le=32768, allow_inf_nan=False)
    y: float | None = Field(default=None, ge=0, le=32768, allow_inf_nan=False)
    target: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    text: str | None = Field(default=None, max_length=12000)
    key: str | None = Field(default=None, max_length=100)
    url: str | None = Field(default=None, max_length=4096)
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | None = Field(default=None, ge=1, le=2000)
    seconds: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    reason: str = Field(default="", max_length=2000)
    button: Literal["left", "right", "middle"] = "left"
    end_x: float | None = Field(default=None, ge=0, le=32768, allow_inf_nan=False)
    end_y: float | None = Field(default=None, ge=0, le=32768, allow_inf_nan=False)
    duration: float = Field(default=0.5, ge=0.1, le=3)
    app: str | None = Field(default=None, max_length=200)
    expected: str = Field(default="", max_length=2000)
    memory: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def validate_action(self):
        if (self.x is None) != (self.y is None):
            raise ValueError("x 與 y 必須同時提供")
        if self.type in ("click", "double_click", "move", "drag") and not self.target and self.x is None:
            raise ValueError("點擊需要 target 或 x/y")
        if self.type == "drag" and (self.end_x is None or self.end_y is None):
            raise ValueError("拖曳需要 end_x 與 end_y")
        if self.type == "open_app" and (not self.app or not self.app.strip()):
            raise ValueError("開啟應用程式需要 app")
        if self.type == "select_option" and (not self.target or self.text is None):
            raise ValueError("選擇選項需要 target 與 text")
        if self.type == "upload_file" and (not self.target or not self.text):
            raise ValueError("上傳檔案需要 target 與 text（已允許的檔案路徑）")
        if self.type == "switch_tab" and not self.text:
            raise ValueError("切換分頁需要 text（分頁 ID）")
        if self.type == "type" and self.text is None:
            raise ValueError("輸入動作需要 text")
        if self.type == "key" and not self.key:
            raise ValueError("按鍵動作需要 key")
        if self.type == "navigate" or (self.type == "new_tab" and self.url):
            p = urlsplit(self.url or "")
            if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
                raise ValueError("導覽只允許不含帳密的 HTTP(S) 網址")
        if self.type == "scroll" and not self.direction:
            raise ValueError("捲動需要 direction")
        if self.type in ("done", "ask_user") and not self.text:
            raise ValueError("完成時請提供結果摘要")
        return self


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=1, max_length=8000)
    target: Literal["browser", "desktop"] = "browser"
    approval_mode: Literal["always", "auto"] = "always"
    max_steps: int = Field(default=30, ge=1, le=200)
    start_url: str = Field(default="about:blank", max_length=4096)
    browser_visible: bool = False
    desktop_pid: int | None = Field(default=None, gt=0, strict=True)
    desktop_window_id: int | None = Field(default=None, gt=0, strict=True)
    allowed_uploads: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def paired_desktop_identity(self):
        if (self.desktop_pid is None) != (self.desktop_window_id is None):
            raise ValueError("請同時指定背景應用程式 PID 與視窗 ID")
        if self.target == "browser" and self.desktop_pid is not None:
            raise ValueError("瀏覽器任務不可指定桌面視窗")
        return self

    @field_validator("task")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("請輸入任務")
        return value.strip()

    @field_validator("start_url")
    @classmethod
    def start_http(cls, value: str) -> str:
        if value in ("", "about:blank"):
            return "about:blank"
        p = urlsplit(value)
        if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
            raise ValueError("起始網頁必須是 HTTP(S) 網址")
        return value


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=8000)
