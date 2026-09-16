"""Bounded computer actions, isolated browser sessions, and local desktop capture.

The model supplies an Action, never a selector or executable browser script.
All JavaScript below is fixed observation/guard code owned by this application.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import importlib.util
import io
import math
import mimetypes
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit
from .browser_text import collect_page_text
from .browser_pointer import BrowserPointer, handle_point, validate_hit
from .motion import MotionPolicy
from .key_contract import canonical_key_chord


if TYPE_CHECKING:
    from .schemas import Action


def validate_navigation_url(url: str) -> str:
    """Limit navigation to normal web pages, without embedded credentials."""
    if url == "about:blank":
        return url
    if not isinstance(url, str) or not url or any(ord(c) < 32 for c in url):
        raise ValueError("網址無效。請使用完整的 http:// 或 https:// 網址。")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("僅允許 http://、https:// 和 about:blank；不允許檔案或程式碼網址。")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("網址不可包含帳號或密碼。")
    return url


def _installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _mac_permissions() -> tuple[bool | None, bool | None]:
    """Read preflight status only; do not trigger macOS permission dialogs."""
    if platform.system() != "Darwin":
        return None, None
    trusted = capture = None
    try:
        services = ctypes.CDLL(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
        check = services.AXIsProcessTrusted
        check.restype = ctypes.c_bool
        check.argtypes = []
        trusted = bool(check())
    except (OSError, AttributeError):
        pass
    try:
        graphics = ctypes.CDLL(
            "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
        )
        check = graphics.CGPreflightScreenCaptureAccess
        check.restype = ctypes.c_bool
        check.argtypes = []
        capture = bool(check())
    except (OSError, AttributeError):
        pass
    return trusted, capture


async def capabilities() -> dict:
    system = platform.system()
    desktop = all(_installed(name) for name in ("pyautogui", "pyperclip", "PIL"))
    hint = "桌面模式會控制主要螢幕；將滑鼠移到螢幕角落可觸發緊急停止。"
    if not desktop:
        hint = "桌面模式需要安裝 pyautogui、pyperclip 與 Pillow。"
    elif system == "Darwin":
        trusted, capture = _mac_permissions()
        missing = []
        if trusted is False:
            missing.append("輔助使用")
        if capture is False:
            missing.append("螢幕錄製")
        if missing:
            desktop = False
            hint = "請在 macOS 系統設定 → 隱私權與安全性，允許啟動伺服器的終端機「" + "、".join(missing) + "」，然後重新啟動伺服器。"
    elif system == "Linux" and not os.environ.get("DISPLAY"):
        desktop = False
        hint = "桌面模式需要可存取的 X11 DISPLAY；目前未偵測到顯示環境。"
    return {"browser": _installed("playwright"), "desktop": desktop, "platform": system, "desktop_hint": hint}


_KEY_ALIASES = {
    "CONTROL": "CTRL", "COMMAND": "CMD", "META": "CMD", "SUPER": "CMD",
    "OPTION": "ALT", "RETURN": "ENTER", "ESCAPE": "ESC", "SPACEBAR": "SPACE",
    "ARROWUP": "UP", "ARROWDOWN": "DOWN", "ARROWLEFT": "LEFT", "ARROWRIGHT": "RIGHT",
    "PGUP": "PAGEUP", "PGDN": "PAGEDOWN", "DEL": "DELETE", "BACK": "BACKSPACE",
}
_KEY_NAMES = {
    "CTRL": ("Control", "ctrl"), "CMD": ("Meta", "command"),
    "ALT": ("Alt", "alt"), "SHIFT": ("Shift", "shift"),
    "ENTER": ("Enter", "enter"), "ESC": ("Escape", "esc"),
    "TAB": ("Tab", "tab"), "SPACE": ("Space", "space"),
    "UP": ("ArrowUp", "up"), "DOWN": ("ArrowDown", "down"),
    "LEFT": ("ArrowLeft", "left"), "RIGHT": ("ArrowRight", "right"),
    "HOME": ("Home", "home"), "END": ("End", "end"),
    "PAGEUP": ("PageUp", "pageup"), "PAGEDOWN": ("PageDown", "pagedown"),
    "BACKSPACE": ("Backspace", "backspace"), "DELETE": ("Delete", "delete"),
    "INSERT": ("Insert", "insert"), "PLUS": ("+", "+"),
}


def _keys(chord: str, browser: bool) -> list[str]:
    canonical_key_chord(chord, {"target": "browser" if browser else "desktop", "platform": platform.system()})
    parts = chord.split("+")
    if not 1 <= len(parts) <= 5 or any(not p.strip() for p in parts):
        raise ValueError("按鍵組合無效，例如 CTRL+L、CMD+L 或 ENTER。")
    result = []
    for part in parts:
        key = _KEY_ALIASES.get(part.strip().upper(), part.strip().upper())
        if key in _KEY_NAMES:
            mapped = _KEY_NAMES[key][0 if browser else 1]
            if not browser and key == "CMD" and platform.system() != "Darwin":
                mapped = "win"
            result.append(mapped)
        elif re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", key):
            result.append(key if browser else key.lower())
        elif len(key) == 1 and key.isascii() and key.isprintable():
            result.append(key.lower())
        else:
            raise ValueError(f"不支援的按鍵：{part[:30]}")
    return result


class _Targets:
    def __init__(self) -> None:
        self._observation: dict | None = None
        self._targets: dict[str, dict] = {}

    def set_observation(self, observation: dict) -> None:
        self._observation = observation
        self._targets = {
            item["id"]: dict(item) for item in observation.get("elements", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    def _point(self, action: Action) -> tuple[int, int]:
        if self._observation is None:
            raise ValueError("請先擷取畫面，再執行座標動作。")
        x, y = action.x, action.y
        if action.target:
            target = self._targets.get(action.target)
            if target is None:
                raise ValueError("找不到這個畫面元素；請重新觀察畫面。")
            x, y = target.get("x"), target.get("y")
        return self._checked_point(x, y)

    def _checked_point(self, x: float | None, y: float | None) -> tuple[int, int]:
        if self._observation is None:
            raise ValueError("請先擷取畫面，再執行座標動作。")
        width, height = self._observation["width"], self._observation["height"]
        if not isinstance(x, (float, int)) or not isinstance(y, (float, int)):
            raise ValueError("此動作需要有效的元素 ID 或 x、y 座標。")
        if not math.isfinite(x) or not math.isfinite(y) or not (0 <= x < width and 0 <= y < height):
            raise ValueError("座標超出目前截圖範圍。請重新觀察畫面。")
        return min(round(x), width - 1), min(round(y), height - 1)


_SELECTOR = "a[href],button,input:not([type=hidden]),textarea,select,[role=button],[role=link],[role=checkbox],[role=tab],[role=menuitem],[contenteditable=true],[tabindex]:not([tabindex='-1'])"
_ELEMENT_INFO = """(el, state) => {
  if (el.closest('[data-computeruse-cursor]')) return null;
  const tag = el.tagName.toLowerCase();
  const style = getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none') return null;
  const inputType = (el.getAttribute('type') || '').toLowerCase();
  const role = el.getAttribute('role') || ({a:'link',button:'button',textarea:'textbox',select:'combobox',input:inputType==='checkbox'?'checkbox':inputType==='radio'?'radio':'textbox'})[tag] || tag;
  const labels = el.labels ? Array.from(el.labels).map(l=>l.innerText).join(' ') : '';
  const text = (el.getAttribute('aria-label') || labels || el.innerText || el.getAttribute('placeholder') || el.getAttribute('title') || '').replace(/\\s+/g,' ').trim().slice(0,250);
  const options = tag==='select' ? Array.from(el.options).slice(0,80).map(o=>({label:o.label,value:o.value,selected:o.selected,disabled:o.disabled})) : undefined;
  const value = ['password','file'].includes(inputType) ? undefined : ('value' in el ? String(el.value).slice(0,1200) : el.isContentEditable ? el.innerText.slice(0,1200) : undefined);
  const selected_options = tag==='select' ? Array.from(el.selectedOptions).map(o=>({label:o.label,value:o.value})) : undefined;
  let href;
  if (tag==='a') { try { const url=new URL(el.href,document.baseURI); if (['http:','https:'].includes(url.protocol)&&!url.username&&!url.password) href=url.href; } catch {} }
  if (!state.nodes.has(el)) state.nodes.set(el, state.prefix + (++state.next));
  return {id:state.nodes.get(el),role,text,value,href,disabled:!!el.disabled,checked:typeof el.checked==='boolean'?el.checked:undefined, input_type:inputType || undefined, options,selected_options};
}"""
_GUARD_SCRIPT = """(() => {
  document.addEventListener('click', event => {
    const anchor = event.composedPath().find(el => el instanceof HTMLAnchorElement);
    if (!anchor) return;
    const url = new URL(anchor.href, document.baseURI);
    const safeDownload = anchor.hasAttribute('download') && ['blob:', 'data:'].includes(url.protocol);
    if (!safeDownload && !['http:', 'https:'].includes(url.protocol)) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
})();"""


class BrowserDriver(_Targets):
    _SCOPED_RESPONSE_LIMIT = 8 * 1024 * 1024
    _SCOPED_REQUEST_TIMEOUT_MS = 15000

    def __init__(self, start_url: str = "about:blank", headless: bool = True):
        super().__init__()
        self.start_url = validate_navigation_url(start_url)
        self.headless = headless
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._handles: dict[str, Any] = {}
        self._document_counter = 0
        self._frame_states: dict[Any, Any] = {}
        self._tabs: dict[str, Any] = {}
        self._tab_counter = 0
        self._allowed_uploads: dict[Path, tuple[int, int, int, int, int]] = {}
        self._download_dir: Path | None = None
        self._download_dir_identity: tuple[int, int] | None = None
        self._download_tasks: dict[asyncio.Task, Any] = {}
        self._download_errors: list[str] = []
        self.downloads: list[dict] = []
        self.download_paths: dict[str, Path] = {}
        self._closing = False
        self.motion_config = MotionPolicy()
        self._motion_interrupted = asyncio.Event()
        self._pointers: dict[Any, BrowserPointer] = {}
        self._actions: set[asyncio.Task] = set()
        self._action_guard: Callable[[], None] | None = None
        self._allowed_origin: str | None = None
        self._allowed_origin_identity: tuple[str, str, int] | None = None
        self._start_attempted = False

    @staticmethod
    def _http_origin(url: str) -> tuple[str, str, int]:
        """Strict HTTP origin comparison without importing the candidate core."""
        try:
            if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
                raise ValueError
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                raise ValueError
            host = parsed.hostname
            if "%" in host or "*" in host:
                raise ValueError
            host = host.encode("idna").decode("ascii").lower() if ":" not in host else host.lower()
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, host, port
        except (TypeError, ValueError, UnicodeError):
            raise ValueError("網址沒有可確認的 HTTP(S) 來源。") from None

    @property
    def allowed_origin(self) -> str | None:
        return self._allowed_origin

    def configure_allowed_origin(self, origin: str) -> None:
        """Pin routed HTTP requests to one origin before any browser startup.

        Scoped contexts disable HTTP redirects, WebSockets and Service Workers.
        Responses are buffered, capped at 8 MiB and have a 15 second timeout;
        streaming is unsupported. This is a Playwright request boundary, not
        an OS/network sandbox or a claim about other browser-managed transports.
        """
        if self._start_attempted or self._allowed_origin is not None:
            raise RuntimeError("網站來源限制必須在啟動前設定一次，不能替換或解除。")
        identity = self._http_origin(origin)
        parsed = urlsplit(origin)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("網站來源限制只能包含協定、主機和連接埠。")
        if self._http_origin(self.start_url) != identity:
            raise ValueError("啟動網址不在指定的網站來源內。")
        scheme, host, port = identity
        host = f"[{host}]" if ":" in host else host
        suffix = "" if port == (443 if scheme == "https" else 80) else f":{port}"
        self._allowed_origin = f"{scheme}://{host}{suffix}"
        self._allowed_origin_identity = identity

    def bind_action_guard(self, callback: Callable[[], None]) -> None:
        """Bind one synchronous authority check for the lifetime of this driver.

        Checks precede action IPC sends; they cannot retract commands Chromium
        has already received or fence autonomous page events. Observation,
        startup and cleanup (including releasing held buttons) remain available.
        """
        import inspect
        if self._action_guard is not None:
            raise RuntimeError("瀏覽器動作權限檢查已綁定，不能替換或解除。")
        if not callable(callback) or inspect.iscoroutinefunction(callback) or inspect.iscoroutinefunction(getattr(callback, "__call__", None)):
            raise TypeError("動作權限檢查必須是同步、無參數的 callable。")
        self._action_guard = callback

    def configure_motion(self, policy: MotionPolicy) -> None:
        if self._context is not None:
            raise RuntimeError("游標設定必須在瀏覽器工作開始前指定。")
        self.motion_config = policy

    def _action_boundary(self) -> None:
        if self._closing:
            raise DriverAbort("瀏覽器工作已停止。")
        if self._motion_interrupted.is_set():
            raise ActionInterrupted("瀏覽器動作已暫停；恢復後需重新觀察。")
        if self._action_guard is not None:
            import inspect
            result = self._action_guard()
            if inspect.isawaitable(result):
                # A synchronous wrapper can still accidentally return a
                # coroutine. Never interpret its unexecuted check as approval.
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("動作權限檢查不能回傳 awaitable。")

    def interrupt_action(self) -> None:
        self._motion_interrupted.set()

    async def resume_actions(self) -> None:
        pending = self._actions - {asyncio.current_task()}
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._closing:
            raise DriverAbort("瀏覽器工作已停止，不能恢復。")
        self._motion_interrupted.clear()

    def _pointer(self, page: Any) -> BrowserPointer:
        if page not in self._pointers:
            self._pointers[page] = BrowserPointer(page, self._motion_interrupted,
                self._action_boundary, DriverAbort, policy=self.motion_config)
        return self._pointers[page]

    async def _pointer_target(self, page: Any, action: Action):
        handle = self._handles.get(action.target) if action.target else None
        if handle is not None:
            if await handle.owner_frame() not in page.frames:
                raise ValueError("元素不屬於目前分頁；請重新觀察。")
            point = await handle_point(handle, page.viewport_size)
            async def validate(point):
                await validate_hit(handle, point)
            await validate(point)
            return point, validate
        return self._point(action), None

    def configure_files(self, allowed_uploads: list[str], download_dir: Path) -> None:
        """Configure explicit per-run files before starting the browser.

        Uploads are limited to 64 MiB each and their identity/content metadata is
        pinned at authorization. Completed downloads survive browser cleanup.
        """
        if self._context is not None:
            raise RuntimeError("檔案授權必須在瀏覽器工作開始前設定。")
        uploads = {}
        for raw in allowed_uploads:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                raise ValueError("允許上傳的檔案必須提供完整絕對路徑。")
            try:
                path = path.resolve(strict=True)
                info = path.stat()
            except OSError as exc:
                raise ValueError("允許上傳的檔案不存在或無法讀取。") from exc
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("允許上傳的路徑必須是一般檔案。")
            if info.st_size > 64 * 1024 * 1024:
                raise ValueError("單一上傳檔案上限為 64 MiB。")
            uploads[path] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        folder = Path(download_dir).expanduser().resolve()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not folder.is_dir():
            raise ValueError("下載位置必須是資料夾。")
        info = folder.stat()
        self._allowed_uploads = uploads
        self._download_dir = folder
        self._download_dir_identity = (info.st_dev, info.st_ino)

    def _upload_payload(self, raw: str) -> dict:
        requested = Path(raw).expanduser()
        if not requested.is_absolute():
            raise ValueError("上傳動作需要已授權檔案的完整絕對路徑。")
        try:
            path = requested.resolve(strict=True)
        except OSError as exc:
            raise ValueError("上傳檔案不存在或路徑已變更。") from exc
        identity = self._allowed_uploads.get(path)
        if identity is None:
            raise ValueError("這個檔案不在本次任務的上傳授權清單。")
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            before = os.fstat(descriptor)
            actual = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            if not stat.S_ISREG(before.st_mode) or actual != identity:
                raise ValueError("上傳檔案在授權後已被替換或修改；請重新授權。")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                content = source.read(64 * 1024 * 1024 + 1)
                after = os.fstat(source.fileno())
            if len(content) > 64 * 1024 * 1024 or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != identity:
                raise ValueError("讀取上傳檔案時內容改變；已取消上傳。")
            # The browser receives bytes from the verified descriptor. It never
            # re-opens a model-provided path after a TOCTOU-sensitive stat check.
            return {"name": path.name, "mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream", "buffer": content}
        except OSError as exc:
            raise ValueError("無法安全讀取已授權檔案，路徑可能已變更。") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _on_download(self, download: Any) -> None:
        task = asyncio.create_task(self._save_download(download))
        self._download_tasks[task] = download
        def finished(done: asyncio.Task) -> None:
            self._download_tasks.pop(done, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def _save_download(self, download: Any) -> None:
        destination = None
        complete = False
        try:
            if self._closing or self._download_dir is None:
                await download.cancel()
                if not self._closing:
                    self._download_errors.append("下載已取消：尚未設定本次任務的下載目錄。")
                return
            folder = self._download_dir
            info = folder.stat()
            if folder.resolve() != folder or (info.st_dev, info.st_ino) != self._download_dir_identity:
                raise ValueError("下載目錄在任務開始後已變更。")
            raw_name = str(download.suggested_filename or "download")
            basename = raw_name.replace("\\", "/").rsplit("/", 1)[-1]
            basename = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", basename).strip(" .")
            basename = "".join(char if char.isprintable() else "_" for char in basename)
            basename = basename.encode("utf-8")[:160].decode("utf-8", "ignore") or "download"
            download_id = "file_" + uuid.uuid4().hex
            destination = folder / f"{download_id}__{basename}"
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
            await asyncio.wait_for(download.save_as(destination), timeout=60)
            if destination.is_symlink() or destination.resolve().parent != folder:
                raise ValueError("下載輸出位置在儲存時發生變更。")
            info = destination.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("下載輸出不是一般檔案。")
            self.download_paths[download_id] = destination
            self.downloads.append({"id": download_id, "name": basename, "size": info.st_size})
            complete = True
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(download.cancel(), timeout=3)
            except Exception:
                pass
            raise
        except Exception as exc:
            try:
                await asyncio.wait_for(download.cancel(), timeout=3)
            except Exception:
                pass
            self._download_errors.append(f"下載未完成（{type(exc).__name__}）。")
        finally:
            if destination is not None and not complete:
                destination.unlink(missing_ok=True)

    async def start(self) -> None:
        self._start_attempted = True
        if self._allowed_origin_identity is not None and self._http_origin(self.start_url) != self._allowed_origin_identity:
            raise ValueError("啟動網址不在已固定的網站來源內。")
        self._closing = False
        self._motion_interrupted.clear()
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("瀏覽器模式需要 playwright。請安裝相依套件並執行 python -m playwright install chromium。") from exc
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=self.headless)
            self._context = await self._browser.new_context(
                viewport={"width": 1280, "height": 800}, device_scale_factor=1,
                accept_downloads=True, permissions=[], service_workers="block",
            )
            self._context.set_default_timeout(7000)
            self._context.set_default_navigation_timeout(25000)
            await self._context.add_init_script(_GUARD_SCRIPT)
            await self._context.route("**/*", self._guard_request)
            if self._allowed_origin_identity is not None:
                # Install before any document can create a connection. A routed
                # socket never connects unless connect_to_server is requested.
                await self._context.route_web_socket("**/*", self._block_websocket)
            self._page = await self._context.new_page()
            self._setup_page(self._page)
            await self._navigate_page(self._page, self.start_url)
        except BaseException as exc:
            await self.close()
            if "Executable doesn't exist" in str(exc):
                raise RuntimeError("尚未安裝 Chromium。請執行 python -m playwright install chromium，然後重試。") from exc
            raise

    async def _navigate_page(self, page: Any, url: str, *, action_path: bool = False) -> bool:
        try:
            if action_path:
                self._action_boundary()
            await page.goto(url, wait_until="domcontentloaded")
            return False
        except Exception as exc:
            # Chromium reports this when navigation starts an attachment rather
            # than replacing the document. The tracked download is the result.
            if "Download is starting" in str(exc):
                await asyncio.sleep(0)
                if self._download_tasks or self.downloads:
                    return True
            raise

    async def _guard_request(self, route: Any) -> None:
        try:
            if route.request.is_navigation_request():
                validate_navigation_url(route.request.url)
            if self._allowed_origin_identity is not None and self._http_origin(route.request.url) != self._allowed_origin_identity:
                raise ValueError("請求超出本次網站來源限制。")
        except ValueError:
            await route.abort("blockedbyclient")
            return
        if self._allowed_origin_identity is not None:
            await self._serve_scoped_request(route)
            return
        await route.continue_()

    async def _serve_scoped_request(self, route: Any) -> None:
        """Do not let Chromium follow redirects outside request interception.

        Playwright continues redirect hops without invoking context.route again.
        Fetch exactly once without retries/redirects and reject every 3xx.
        The public fetch API buffers internally: the body cap limits what we
        forward, not the fetch implementation's peak memory allocation.
        """
        response = None
        try:
            response = await route.fetch(max_redirects=0, max_retries=0,
                                         timeout=self._SCOPED_REQUEST_TIMEOUT_MS)
            if 300 <= response.status < 400:
                raise ValueError("限定來源模式尚不支援 HTTP 重新導向。")
            headers = response.headers
            length = headers.get("content-length")
            if length is not None and (int(length) < 0 or int(length) > self._SCOPED_RESPONSE_LIMIT):
                raise ValueError("回應超過限定來源模式的大小上限。")
            if headers.get("content-type", "").lower().split(";", 1)[0].strip() == "text/event-stream":
                raise ValueError("限定來源模式不支援串流回應。")
            body = await response.body()
            if len(body) > self._SCOPED_RESPONSE_LIMIT:
                raise ValueError("回應超過限定來源模式的大小上限。")
            await route.fulfill(response=response, body=body)
        except Exception:
            # No fallback network request: a failure after POST dispatch is an
            # unknown effect, never permission to retry through Chromium.
            try:
                await route.abort("blockedbyclient")
            except Exception:
                pass  # The context may already be closing.
        finally:
            if response is not None:
                try:
                    await response.dispose()
                except Exception:
                    pass

    async def _block_websocket(self, websocket: Any) -> None:
        await websocket.close(code=1008, reason="WebSockets are disabled in origin-scoped browser sessions")

    def _setup_page(self, page: Any) -> None:
        self._tab_counter += 1
        self._tabs[f"tab_{self._tab_counter}"] = page
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("download", self._on_download)
        page.on("popup", self._on_popup)

    def _on_popup(self, page: Any) -> None:
        self._setup_page(page)
        self._page = page

    def _require_page(self) -> Any:
        if self._page is None or self._page.is_closed():
            pages = self._context.pages if self._context is not None else []
            if not pages:
                raise RuntimeError("瀏覽器尚未啟動，或所有分頁都已關閉。")
            self._page = pages[-1]
        return self._page

    async def prepare_for_action(self) -> None:
        """Browser approval UI cannot steal focus from its isolated context."""
        self._require_page()
        self._action_boundary()

    async def _frame_state(self, frame: Any) -> Any:
        """Private WeakMap IDs survive observations but never cross documents."""
        state = self._frame_states.get(frame)
        if state is not None:
            try:
                if await state.evaluate("state => state.document === document"):
                    return state
            except Exception:
                pass  # Navigation destroyed the old execution context.
            try:
                await state.dispose()
            except Exception:
                pass
            self._frame_states.pop(frame, None)
        self._document_counter += 1
        state = await frame.evaluate_handle("prefix => ({document, prefix, next:0, nodes:new WeakMap()})", f"e{self._document_counter}_")
        self._frame_states[frame] = state
        return state

    async def observe(self) -> dict:
        page = self._require_page()
        pointer = self._pointer(page)
        await pointer.overlay()
        for handle in self._handles.values():
            try:
                await handle.dispose()
            except Exception:
                pass
        self._handles = {}
        for frame, state in list(self._frame_states.items()):
            if frame.is_detached():
                try:
                    await state.dispose()
                except Exception:
                    pass
                self._frame_states.pop(frame, None)
        elements = []
        # ElementHandle.bounding_box is in main-frame viewport coordinates, even
        # for a child frame. IDs refer to these handles rather than CSS selectors.
        for frame in page.frames:
            try:
                state = await self._frame_state(frame)
                handles = await frame.query_selector_all(_SELECTOR)
            except Exception:
                continue
            for handle in handles:
                keep = False
                try:
                    box = await handle.bounding_box()
                    if box is None or box["width"] <= 0 or box["height"] <= 0:
                        continue
                    x = max(0, box["x"])
                    y = max(0, box["y"])
                    right = min(1280, box["x"] + box["width"])
                    bottom = min(800, box["y"] + box["height"])
                    if right <= x or bottom <= y or len(elements) >= 150:
                        continue
                    info = await handle.evaluate(_ELEMENT_INFO, state)
                    if info is None:
                        continue
                    element_id = info["id"]
                    elements.append({"id": element_id, **info, "x": round((x + right) / 2, 1), "y": round((y + bottom) / 2, 1), "width": round(right - x, 1), "height": round(bottom - y, 1), "source": "dom"})
                    self._handles[element_id] = handle
                    keep = True
                except Exception:
                    pass  # A mutating page can remove elements while observed.
                finally:
                    if not keep:
                        try:
                            await handle.dispose()
                        except Exception:
                            pass
        text = await collect_page_text(page)
        screenshot = await page.screenshot(type="png", full_page=False, animations="disabled", timeout=10000)
        tabs = []
        for tab_id, tab in list(self._tabs.items()):
            if tab.is_closed():
                del self._tabs[tab_id]
            else:
                try:
                    tabs.append({"id": tab_id, "title": await tab.title(), "url": tab.url, "active": tab == page})
                except Exception:
                    pass
        tab_text = "\n".join(f"[{tab['id']}] {'目前分頁' if tab['active'] else '分頁'}: {tab['title']} {tab['url']}" for tab in tabs)
        download_text = "\n".join(f"[{item['id']}] 已下載 {item['name']}（{item['size']} bytes）" for item in self.downloads)
        if self._download_tasks:
            download_text += f"\n{len(self._download_tasks)} 個下載仍在處理。"
        download_text += "\n" + "\n".join(self._download_errors[-5:])
        observation = {
            "image": "data:image/png;base64," + base64.b64encode(screenshot).decode("ascii"),
            "width": 1280, "height": 800, "url": page.url,
            "title": await page.title(), "text": text + "\n\n" + tab_text + "\n" + download_text.strip(), "elements": elements, "tabs": tabs,
            "downloads": [dict(item) for item in self.downloads],
            "allowed_uploads": [str(path) for path in self._allowed_uploads],
            "input_transport": "playwright_page_mouse", "agent_cursor_available": True,
            "physical_input_untouched": self.headless, "cursor": pointer.metadata(),
            "perception_exclusions": [{"kind": "executor_cursor",
                                       "bbox": [*pointer.position, 22, 30]}],
        }
        if self._allowed_origin is not None:
            observation.update({"allowed_origin": self._allowed_origin,
                "origin_scope_mode": "http_no_redirects", "redirects_supported": False,
                "websocket": False, "streaming": False,
                "max_response_bytes": self._SCOPED_RESPONSE_LIMIT,
                "request_timeout_ms": self._SCOPED_REQUEST_TIMEOUT_MS,
                "network_sandbox": False})
        self.set_observation(observation)
        return observation

    async def execute(self, action: Action) -> str:
        current = asyncio.current_task()
        self._actions.add(current)
        try:
            self._action_boundary()
            return await self._execute(action)
        finally:
            self._actions.discard(current)

    async def _execute(self, action: Action) -> str:
        page = self._require_page()
        pointer = self._pointer(page)
        kind = action.type
        button = getattr(action, "button", "left")
        if kind == "upload_file":
            handle = self._handles.get(action.target)
            if handle is None:
                raise ValueError("上傳需要目前觀察到的檔案欄位或上傳按鈕元素 ID。")
            # Read and verify authorization before clicking a custom picker.
            payload = await asyncio.to_thread(self._upload_payload, action.text or "")
            native_input = await handle.evaluate("el => el.tagName === 'INPUT' && el.type === 'file'")
            if native_input:
                self._action_boundary()
                await handle.set_input_files(payload)
            else:
                from playwright.async_api import TimeoutError as PlaywrightTimeoutError
                try:
                    point, validate = await self._pointer_target(page, action)
                    async with page.expect_file_chooser(timeout=4000) as opened:
                        await pointer.click(point, validate=validate)
                    chooser = await opened.value
                except PlaywrightTimeoutError as exc:
                    raise ValueError("這個元素沒有開啟檔案選擇器；請重新觀察並選擇正確的上傳按鈕。") from exc
                self._action_boundary()
                await chooser.set_files(payload)
            return f"已選取已授權檔案 {payload['name']}；若頁面需要送出，請繼續操作。"
        if kind == "new_tab":
            url = validate_navigation_url(action.url or "about:blank")
            self._action_boundary()
            self._page = await self._context.new_page()
            self._setup_page(self._page)
            await self._navigate_page(self._page, url, action_path=True)
            self._handles, self._targets = {}, {}
            return "已開啟新分頁。"
        if kind in {"switch_tab", "close_tab"}:
            tab = self._tabs.get(action.text) if action.text else page
            if tab is None or tab.is_closed():
                raise ValueError("分頁 ID 已失效；請重新觀察。")
            if kind == "close_tab":
                self._action_boundary()
                await tab.close()
                pages = [p for p in self._context.pages if not p.is_closed()]
                if not pages:
                    self._action_boundary()
                    self._page = await self._context.new_page()
                    self._setup_page(self._page)
                else:
                    self._page = pages[-1]
            else:
                self._action_boundary()
                self._page = tab
                await tab.bring_to_front()
            self._handles, self._targets = {}, {}
            return "已關閉分頁。" if kind == "close_tab" else "已切換分頁。"
        if kind in {"back", "forward"}:
            self._action_boundary()
            await (page.go_back(wait_until="domcontentloaded") if kind == "back" else page.go_forward(wait_until="domcontentloaded"))
            self._handles, self._targets = {}, {}
            return "已返回上一頁。" if kind == "back" else "已前往下一頁。"
        if kind == "select_option":
            handle = self._handles.get(action.target)
            if handle is None:
                raise ValueError("請使用目前觀察到的原生下拉選單元素 ID。")
            options = await handle.evaluate("el => el.tagName==='SELECT' ? Array.from(el.options).map(o=>({value:o.value,label:o.label})) : null")
            if options is None:
                raise ValueError("此元素不是原生下拉選單；自訂選單請用 click 和 key。")
            matching = [option for option in options if option["value"] == action.text or option["label"] == action.text]
            if len(matching) != 1:
                raise ValueError("找不到唯一對應的選項；請使用觀察到的完整標籤或值。")
            self._action_boundary()
            await handle.select_option(value=matching[0]["value"])
            return "已選擇指定選項。"
        if kind == "navigate":
            downloading = await self._navigate_page(page, validate_navigation_url(action.url or ""), action_path=True)
            self._targets = {}
            self._handles = {}
            return "已開始下載；請觀察下載清單確認完成。" if downloading else "已前往指定網頁。"
        if kind in {"click", "double_click", "type"}:
            handle = self._handles.get(action.target) if action.target else None
            if handle is not None:
                if kind == "type":
                    if (await handle.get_attribute("type") or "").lower() == "file":
                        raise ValueError("檔案欄位請使用 upload_file，並指定本次已授權的完整檔案路徑。")
                    self._action_boundary()
                    await handle.fill(action.text or "")
                else:
                    href = await handle.get_attribute("href")
                    is_download = await handle.get_attribute("download") is not None
                    safe_download = is_download and urlsplit(href or "").scheme.lower() in {"blob", "data"}
                    if href and urlsplit(href).scheme and urlsplit(href).scheme.lower() not in {"http", "https"} and not safe_download:
                        raise ValueError("不允許點擊檔案、程式碼或其他非網頁協定的連結。")
                    point, validate = await self._pointer_target(page, action)
                    await pointer.click(point, button=button, count=2 if kind == "double_click" else 1, validate=validate)
            elif kind == "type" and not action.target and action.x is None and action.y is None:
                self._action_boundary()
                await page.keyboard.insert_text(action.text or "")
            else:
                point, validate = await self._pointer_target(page, action)
                await pointer.click(point, count=2 if kind == "double_click" else 1, button=button if kind != "type" else "left", validate=validate)
                if kind == "type":
                    self._action_boundary()
                    await page.keyboard.insert_text(action.text or "")
            return {"click": "已點擊。", "double_click": "已雙擊。", "type": "已輸入文字。"}[kind]
        if kind == "move":
            point, _ = await self._pointer_target(page, action)
            await pointer.move(point)
            return "已移動滑鼠。"
        if kind == "drag":
            start, validate = await self._pointer_target(page, action)
            end = self._checked_point(action.end_x, action.end_y)
            await pointer.drag(start, end, button=button, duration=action.duration, validate=validate)
            return "已拖曳。"
        if kind == "key":
            self._action_boundary()
            await page.keyboard.press("+".join(_keys(action.key or "", browser=True)))
            return "已按下指定按鍵。"
        if kind == "scroll":
            if action.target or action.x is not None or action.y is not None:
                point, _ = await self._pointer_target(page, action)
                await pointer.move(point)
            amount = action.amount if action.amount is not None else 500
            direction = action.direction or "down"
            if direction not in {"up", "down", "left", "right"}:
                raise ValueError("不支援的捲動方向。")
            dx = amount * (-1 if direction == "left" else 1) if direction in {"left", "right"} else 0
            dy = amount * (-1 if direction == "up" else 1) if direction in {"up", "down"} else 0
            self._action_boundary()
            await page.mouse.wheel(dx, dy)
            return "已捲動畫面。"
        if kind == "wait":
            await asyncio.sleep(min(max(action.seconds or 1, 0), 10))
            return "等待完成。"
        if kind == "done":
            return action.text or "任務完成。"
        raise ValueError(f"瀏覽器不支援此動作：{kind}")

    async def close(self) -> None:
        self._closing = True
        self.interrupt_action()
        actions = self._actions - {asyncio.current_task()}
        for action in actions:
            action.cancel()
        if actions:
            await asyncio.gather(*actions, return_exceptions=True)
        for pointer in self._pointers.values():
            try:
                await pointer.close()
            except Exception:
                pass  # Closing the whole context below releases its input state.
        if self._download_tasks:
            _, pending = await asyncio.wait(tuple(self._download_tasks), timeout=2)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for resource in (self._context, self._browser):
            if resource is not None:
                try:
                    await resource.close()
                except Exception:
                    pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._page = self._context = self._browser = self._playwright = None
        self._handles = {}
        self._frame_states = {}
        self._tabs = {}
        self._pointers = {}
        self._targets = {}
        self._observation = None


class DesktopDriver(_Targets):
    def __init__(self):
        super().__init__()
        self._gui: Any = None
        self._clipboard: Any = None
        self._pending: set[asyncio.Task] = set()
        self._closed = False
        self._stopping = threading.Event()
        self._pressed_buttons: set[str] = set()
        self._foreground_pid: int | None = None
        self._pasteboard_api: Any = None
        self._quartz: Any = None
        self._mac_keycodes: dict[str, int | None] = {}
        self._interrupted = threading.Event()

    def interrupt_action(self) -> None:
        """Pause/steering can stop motion without permanently closing the driver."""
        self._interrupted.set()

    async def resume_actions(self) -> None:
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        if self._stopping.is_set() or self._closed:
            raise DriverAbort("桌面工作已停止，不能恢復輸入。")
        self._interrupted.clear()

    async def _run_sync(self, operation: Callable, *args: Any) -> Any:
        if self._closed:
            raise RuntimeError("桌面工作階段已關閉。")
        task = asyncio.create_task(asyncio.to_thread(operation, *args))
        self._pending.add(task)
        def finished(done: asyncio.Task) -> None:
            self._pending.discard(done)
            # If the outer coroutine was cancelled, nobody else may retrieve a
            # worker exception. Reading it here prevents an orphaned-task log.
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        # On cancellation, close() waits for this bounded operation, including
        # clipboard restoration, before another session may control the desktop.
        try:
            return await asyncio.shield(task)
        except Exception as exc:
            if type(exc).__name__ == "FailSafeException":
                raise DriverAbort("FailSafeException：滑鼠位於螢幕角落，已立即停止桌面工作。") from exc
            raise

    async def start(self) -> None:
        status = await capabilities()
        if not status["desktop"]:
            raise RuntimeError(status["desktop_hint"])
        try:
            import pyautogui
            import pyperclip
        except Exception as exc:
            raise RuntimeError("無法連線到桌面。請確認螢幕環境、相依套件與系統權限。") from exc
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.1
        self._gui, self._clipboard = pyautogui, pyperclip
        self._closed = False
        self._stopping.clear()
        self._interrupted.clear()
        if platform.system() == "Darwin":
            try:
                import AppKit
                import Quartz
                self._pasteboard_api = AppKit
                self._quartz = Quartz
                self._mac_keycodes = dict(pyautogui.platformModule.keyboardMapping)
            except ImportError:
                pass

    def _require_gui(self) -> Any:
        if self._gui is None or self._closed:
            raise RuntimeError("桌面工作階段尚未啟動或已關閉。")
        return self._gui

    def _capture(self) -> dict:
        gui = self._require_gui()
        width, height = map(int, gui.size())
        context = self._desktop_context(width, height)
        self._foreground_pid = context.get("pid")
        screenshot = gui.screenshot()
        # macOS Retina captures can be twice the logical mouse-coordinate size.
        if screenshot.size != (width, height):
            from PIL import Image
            screenshot = screenshot.resize((width, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        screenshot.save(buffer, format="PNG")
        return {
            "image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"),
            "width": width, "height": height, "url": "", "title": context.get("title") or "本機桌面（主要螢幕）",
            "text": "主要螢幕截圖；所有座標使用截圖像素。\n" + context.get("text", ""),
            "elements": context.get("elements", []),
            "accessibility": {key: value for key, value in context.items() if key not in {"elements", "text", "pid"}},
            "input_transport": "desktop_os_pointer",
            "text_input_transport": "unicode_clipboard_paste",
        }

    def _desktop_context(self, width: int, height: int) -> dict:
        from .accessibility import focused_application_pid, read_accessibility
        try:
            result = read_accessibility(width, height)
        except Exception as exc:
            result = {"available": False, "elements": [], "text": "", "hint": f"Accessibility 無法讀取（{type(exc).__name__}）。"}
        if platform.system() == "Darwin":
            if not result.get("pid"):
                result["pid"] = focused_application_pid()
            try:
                from AppKit import NSWorkspace
                app = NSWorkspace.sharedWorkspace().frontmostApplication()
                # NSWorkspace may cache another app when no Cocoa run loop is
                # serviced. It may supply a missing title only if fresh AX agrees.
                if app and result.get("pid") == int(app.processIdentifier()) and not result.get("title"):
                    result["title"] = str(app.localizedName())
            except Exception:
                pass
        return result

    def _restore_focus(self) -> None:
        """Approval in the dashboard may have stolen focus from the observed app."""
        if platform.system() != "Darwin":
            return
        from .accessibility import focused_application_pid
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication
        if self._foreground_pid is None:
            raise RuntimeError("無法確認剛才觀察的應用程式；請重新觀察桌面。")
        active_pid = focused_application_pid()
        if active_pid is None:
            raise RuntimeError("無法確認目前前景應用程式，已取消這次輸入。")
        if active_pid == self._foreground_pid:
            return
        target = NSRunningApplication.runningApplicationWithProcessIdentifier_(self._foreground_pid)
        if target is None or target.isTerminated():
            raise RuntimeError("剛才觀察的應用程式已結束；請重新觀察桌面。")
        if not target.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            raise RuntimeError("無法還原剛才觀察的應用程式焦點；已停止這次動作。")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if focused_application_pid(attempts=1, timeout=0.2) == self._foreground_pid:
                time.sleep(0.12)
                return
            time.sleep(0.05)
        raise RuntimeError("應用程式尚未取得焦點；已停止這次動作，避免輸入到錯誤視窗。")

    async def observe(self) -> dict:
        observation = await self._run_sync(self._capture)
        self.set_observation(observation)
        return observation

    async def prepare_for_action(self) -> None:
        """Restore the approved target app before taking a fresh observation."""
        def prepare():
            self._require_gui().failSafeCheck()
            if self._stopping.is_set():
                raise DriverAbort("桌面工作已停止。")
            self._restore_focus()
        await self._run_sync(prepare)

    def _keyboard_boundary(self) -> None:
        self._require_gui().failSafeCheck()
        if self._stopping.is_set():
            raise DriverAbort("桌面工作已停止。")
        if self._interrupted.is_set():
            raise ActionInterrupted("目前動作已因暫停或修改任務而中斷；繼續前請重新觀察。")

    def _hotkey(self, *keys: str) -> None:
        """Post macOS chords with explicit modifier flags and balanced releases.

        PyAutoGUI's macOS events infer modifiers from asynchronous system state;
        that can turn CMD+A into a literal 'a'. Public Quartz flags make each
        event's intended modifier state explicit.
        """
        gui, quartz = self._require_gui(), self._quartz
        if quartz is None or any(self._mac_keycodes.get(key) is None for key in keys):
            self._keyboard_boundary()
            gui.hotkey(*keys)
            return
        masks = {"command": quartz.kCGEventFlagMaskCommand, "shift": quartz.kCGEventFlagMaskShift,
                 "ctrl": quartz.kCGEventFlagMaskControl, "alt": quartz.kCGEventFlagMaskAlternate}
        sequence = list(keys)
        if any(key in '~!@#$%^&*()_+{}|:"<>?' for key in keys) and "shift" not in sequence:
            sequence.insert(0, "shift")
        flags, held = 0, []

        def post(code, down):
            event = quartz.CGEventCreateKeyboardEvent(None, code, down)
            if event is None:
                raise RuntimeError("macOS 無法建立鍵盤事件。")
            quartz.CGEventSetFlags(event, flags)
            quartz.CGEventPost(quartz.kCGHIDEventTap, event)
            # Releases must finish even after interruption; checks only occur
            # before key-down. The interval still gives real events a cadence.
            time.sleep(0.035)

        try:
            for key in sequence:
                self._keyboard_boundary()
                flags |= masks.get(key, 0)
                code = self._mac_keycodes[key]
                held.append((key, code))
                post(code, True)
        finally:
            # Even a fail-safe/stop must release every key we pressed. No new
            # key-down or pointer movement can be emitted during this cleanup.
            first_error = None
            for key, code in reversed(held):
                flags &= ~masks.get(key, 0)
                try:
                    post(code, False)
                except Exception as exc:
                    first_error = first_error or exc
            if first_error:
                raise DriverAbort("macOS 未能完整釋放鍵盤按鍵；已中止整個桌面任務，避免重試輸入。") from first_error

    def _paste(self, text: str) -> None:
        gui = self._require_gui()
        if self._pasteboard_api is not None:
            self._paste_native(text)
            return
        clipboard = self._clipboard
        old_text = clipboard.paste()
        try:
            clipboard.copy(text)
            self._hotkey("command" if platform.system() == "Darwin" else "ctrl", "v")
            time.sleep(0.2)  # Give the destination time to consume clipboard data.
        finally:
            # Respect a user's concurrent copy instead of overwriting it.
            if clipboard.paste() == text:
                clipboard.copy(old_text)

    def _paste_native(self, text: str) -> None:
        """Preserve rich text, images, URLs and multiple items on macOS."""
        api = self._pasteboard_api
        board = api.NSPasteboard.generalPasteboard()
        saved = []
        for item in board.pasteboardItems() or []:
            copied = api.NSPasteboardItem.alloc().init()
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is None:
                    raise RuntimeError("無法完整備份目前剪貼簿，已取消輸入。")
                copied.setData_forType_(data, kind)
            saved.append(copied)
        board.clearContents()
        paste_count = board.changeCount()
        try:
            if not board.setString_forType_(text, api.NSPasteboardTypeString):
                raise RuntimeError("無法將文字寫入剪貼簿。")
            paste_count = board.changeCount()
            self._hotkey("command", "v")
            time.sleep(0.2)
        finally:
            if board.changeCount() == paste_count:
                board.clearContents()
                if saved and not board.writeObjects_(saved):
                    raise RuntimeError("文字已貼上，但系統未能還原原有剪貼簿內容。")

    def _open_app(self, app: str) -> str:
        app = app.strip()
        if not app or app.startswith("-") or any(ord(c) < 32 for c in app):
            raise ValueError("請提供應用程式名稱，不可附加參數或指令。")
        system = platform.system()
        if system == "Darwin":
            completed = subprocess.run(["/usr/bin/open", "-a", app], capture_output=True, timeout=10, check=False)
            if completed.returncode:
                raise RuntimeError("找不到或無法開啟指定應用程式，請使用 macOS 應用程式的正確名稱。")
        elif system == "Windows":
            resolved = shutil.which(app)
            if not resolved or not resolved.lower().endswith(".exe"):
                raise ValueError("Windows 請提供已安裝、可找到的 .exe 應用程式名稱或完整路徑。")
            subprocess.Popen([resolved], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            resolved = shutil.which(app)
            if not resolved:
                raise ValueError("Linux 請提供已安裝應用程式的可執行檔名稱，不可附加參數。")
            subprocess.Popen([resolved], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self._targets = {}
        self._foreground_pid = None
        return f"已請求開啟 {app}。下一次觀察會確認是否啟動。"

    def _open_url(self, url: str) -> str:
        url = validate_navigation_url(url)
        if url == "about:blank":
            raise ValueError("桌面導覽需要 http:// 或 https:// 網址。")
        system = platform.system()
        if system == "Windows":
            os.startfile(url)
        else:
            opener = "/usr/bin/open" if system == "Darwin" else shutil.which("xdg-open")
            if not opener:
                raise RuntimeError("此系統缺少 xdg-open，請改由瀏覽器網址列輸入網址。")
            result = subprocess.run([opener, url], capture_output=True, timeout=10, check=False)
            if result.returncode:
                raise RuntimeError("系統無法使用預設瀏覽器開啟網址。")
        self._targets = {}
        self._foreground_pid = None
        return "已請求預設瀏覽器開啟網址；下一次觀察會確認結果。"

    def _release_buttons(self) -> None:
        gui = self._gui
        if gui is None:
            return
        for button in tuple(self._pressed_buttons):
            try:
                # Releasing a held button must still succeed if the mouse is in
                # a fail-safe corner. This bypass only emits button-up, no move.
                x, y = gui.position()
                gui.platformModule._mouseUp(x, y, button)
            finally:
                self._pressed_buttons.discard(button)

    def _execute(self, action: Action) -> str:
        gui = self._require_gui()
        kind = action.type
        gui.failSafeCheck()
        if self._stopping.is_set():
            raise DriverAbort("桌面工作已停止。")
        self._keyboard_boundary()
        if kind == "open_app":
            return self._open_app(action.app or "")
        if kind == "navigate":
            return self._open_url(action.url or "")
        if kind not in {"navigate", "back", "forward", "select_option", "new_tab", "switch_tab", "close_tab"}:
            self._restore_focus()
        if self._stopping.is_set():
            raise DriverAbort("桌面工作已停止。")
        button = getattr(action, "button", "left")
        if kind in {"click", "double_click"}:
            x, y = self._point(action)
            if kind == "double_click":
                gui.doubleClick(x, y, interval=0.12, button=button)
            else:
                gui.click(x, y, button=button)
            return "已雙擊。" if kind == "double_click" else "已點擊。"
        if kind == "move":
            gui.moveTo(*self._point(action), duration=min(action.duration, 3))
            return "已移動滑鼠。"
        if kind == "drag":
            start = self._point(action)
            end = self._checked_point(action.end_x, action.end_y)
            gui.moveTo(*start)
            self._pressed_buttons.add(button)
            try:
                gui.mouseDown(button=button)
                gui.dragTo(*end, duration=max(0.2, min(action.duration, 3)), button=button, mouseDownUp=False)
            finally:
                self._release_buttons()
            return "已拖曳。"
        if kind == "type":
            if action.target or action.x is not None or action.y is not None:
                gui.click(*self._point(action))
            self._paste(action.text or "")
            return "已貼上文字，並還原原有剪貼簿文字。"
        if kind == "key":
            keys = _keys(action.key or "", browser=False)
            if any(key not in gui.KEYBOARD_KEYS for key in keys):
                raise ValueError("此作業系統不支援指定的按鍵組合。")
            self._hotkey(*keys)
            return "已按下指定按鍵。"
        if kind == "scroll":
            if action.target or action.x is not None or action.y is not None:
                gui.moveTo(*self._point(action))
            amount = action.amount if action.amount is not None else 500
            # Browser amounts are pixels; desktop APIs accept wheel notches.
            notches = max(1, round(amount / 100))
            direction = action.direction or "down"
            if direction in {"left", "right"}:
                if platform.system() == "Windows":
                    raise ValueError("此桌面驅動在 Windows 上不支援水平捲動。")
                gui.hscroll(notches if direction == "right" else -notches)
            elif direction in {"up", "down"}:
                gui.scroll(notches if direction == "up" else -notches)
            else:
                raise ValueError("不支援的捲動方向。")
            return "已捲動畫面。"
        raise ValueError(f"桌面不支援此動作：{kind}。請使用畫面元素、座標與按鍵操作。")

    async def execute(self, action: Action) -> str:
        self._require_gui()
        if action.type == "wait":
            await asyncio.sleep(min(max(action.seconds or 1, 0), 10))
            return "等待完成。"
        if action.type == "done":
            return action.text or "任務完成。"
        return await self._run_sync(self._execute, action)

    async def close(self) -> None:
        # Do not mark closed until active clipboard/pointer operations finish:
        # they still need _require_gui() during final cleanup.
        self._stopping.set()
        self._interrupted.set()
        if self._pending:
            await asyncio.gather(*tuple(self._pending), return_exceptions=True)
        if self._pressed_buttons:
            await asyncio.to_thread(self._release_buttons)
        self._closed = True
        self._gui = self._clipboard = None
        self._targets = {}
        self._observation = None


class DriverAbort(RuntimeError):
    """A fail-safe/stop is terminal and must never be retried by the agent loop."""

    fatal = True


class ActionInterrupted(RuntimeError):
    """The current action was canceled; a resumed task must observe again."""

    interrupted = True
    fatal = False
