"""Real isolated Chromium file workflows plus adversarial local path checks."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from server.drivers import BrowserDriver


def action(kind, **values):
    return SimpleNamespace(**({"type": kind, "x": None, "y": None, "target": None,
        "text": None, "key": None, "url": None, "direction": None, "amount": None,
        "seconds": None, "button": "left", "duration": 0.2, "end_x": None,
        "end_y": None, "app": None} | values))


def test_upload_requires_explicit_authorization_and_regular_file(tmp_path):
    allowed = tmp_path / "approved.txt"
    allowed.write_text("授權內容", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("private", encoding="utf-8")
    driver = BrowserDriver()
    with pytest.raises(ValueError, match="授權清單"):
        driver._upload_payload(str(allowed))
    driver.configure_files([str(allowed)], tmp_path / "downloads")
    assert driver._upload_payload(str(allowed))["buffer"] == "授權內容".encode()
    with pytest.raises(ValueError, match="授權清單"):
        driver._upload_payload(str(other))
    with pytest.raises(ValueError, match="絕對路徑"):
        driver._upload_payload("approved.txt")
    with pytest.raises(ValueError, match="一般檔案"):
        BrowserDriver().configure_files([str(tmp_path)], tmp_path / "downloads2")


def test_replaced_and_modified_uploads_are_rejected(tmp_path):
    allowed = tmp_path / "approved.txt"
    allowed.write_text("original")
    driver = BrowserDriver()
    driver.configure_files([str(allowed)], tmp_path / "downloads")
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("replacement")
    replacement.replace(allowed)
    with pytest.raises(ValueError, match="替換或修改"):
        driver._upload_payload(str(allowed))
    driver.configure_files([str(allowed)], tmp_path / "downloads")
    allowed.write_text("changed after authorization")
    with pytest.raises(ValueError, match="替換或修改"):
        driver._upload_payload(str(allowed))


def test_upload_symlink_escape_after_authorization_is_rejected(tmp_path):
    approved = tmp_path / "approved.txt"
    approved.write_text("approved")
    private = tmp_path / "private.txt"
    private.write_text("private")
    alias = tmp_path / "selected.txt"
    alias.symlink_to(approved)
    driver = BrowserDriver()
    driver.configure_files([str(alias)], tmp_path / "downloads")
    assert driver._upload_payload(str(alias))["buffer"] == b"approved"
    alias.unlink()
    alias.symlink_to(private)
    with pytest.raises(ValueError, match="授權清單"):
        driver._upload_payload(str(alias))


@pytest.fixture
def file_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/download":
                body = "已下載的報告\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="../../report.txt"')
            else:
                body = '''<!doctype html><html><head><meta charset="utf-8"><title>Files</title></head><body>
                <label>上傳資料<input type="file" id="upload"></label><p id="uploaded">尚未選檔</p>
                <input type="file" id="hidden-upload" style="display:none"><button id="upload-button" onclick="document.querySelector('#hidden-upload').click()">選擇附件</button><p id="hidden-uploaded">尚未選擇附件</p>
                <button id="not-upload" onclick="this.textContent='普通按鈕已點擊'">普通按鈕</button>
                <a href="/download" download>下載報告</a>
                <a id="blob" download="blob-report.txt">下載產生的檔案</a>
                <script>
                document.querySelector('#upload').onchange=async event=>{const file=event.target.files[0];document.querySelector('#uploaded').textContent=file.name+':'+await file.text();};
                document.querySelector('#hidden-upload').onchange=async event=>{const file=event.target.files[0];document.querySelector('#hidden-uploaded').textContent=file.name+':'+await file.text();};
                document.querySelector('#blob').href=URL.createObjectURL(new Blob(['generated contents'],{type:'text/plain'}));
                </script></body></html>'''.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_real_browser_upload_download_and_duplicate_names(tmp_path, file_server):
    pytest.importorskip("playwright.async_api")

    async def run():
        source = tmp_path / "hello.txt"
        source.write_text("繁體中文上傳測試", encoding="utf-8")
        folder = tmp_path / "downloads"
        driver = BrowserDriver(start_url=file_server)
        driver.configure_files([str(source)], folder)
        try:
            await driver.start()
        except RuntimeError as exc:
            if "Chromium" in str(exc):
                pytest.skip(str(exc))
            raise
        try:
            observation = await driver.observe()
            target = next(element for element in observation["elements"] if element.get("input_type") == "file")
            await driver.execute(action("upload_file", target=target["id"], text=str(source)))
            await driver._page.wait_for_function("document.querySelector('#uploaded').textContent.includes('繁體中文上傳測試')")
            assert await driver._page.locator("#uploaded").inner_text() == "hello.txt:繁體中文上傳測試"
            for label in ("下載報告", "下載報告", "下載產生的檔案"):
                observation = await driver.observe()
                link = next(element for element in observation["elements"] if element["text"] == label)
                before = len(driver.downloads)
                await driver.execute(action("click", target=link["id"]))
                deadline = asyncio.get_running_loop().time() + 5
                while len(driver.downloads) == before and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.03)
                assert len(driver.downloads) == before + 1, driver._download_errors
            assert len(driver.downloads) == len(driver.download_paths) == 3
            assert len(set(driver.download_paths.values())) == 3
            assert all(path.parent == folder.resolve() for path in driver.download_paths.values())
            assert all("/" not in item["name"] and "\\" not in item["name"] for item in driver.downloads)
            assert [path.read_text() for path in driver.download_paths.values()] == ["已下載的報告\n", "已下載的報告\n", "generated contents"]
            assert all(item["size"] > 0 for item in driver.downloads)
            assert len((await driver.observe())["downloads"]) == 3
            await driver.execute(action("navigate", url=file_server + "/download"))
            deadline = asyncio.get_running_loop().time() + 5
            while len(driver.downloads) < 4 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.03)
            assert len(driver.downloads) == 4
        finally:
            await driver.close()
        assert len(driver.downloads) == 4
        assert all(path.is_file() for path in driver.download_paths.values())
        assert not driver._download_tasks
    asyncio.run(run())


def test_real_custom_file_picker_and_missing_chooser(tmp_path, file_server):
    pytest.importorskip("playwright.async_api")

    async def run():
        source = tmp_path / "附件.txt"
        source.write_text("自訂檔案選擇器測試", encoding="utf-8")
        driver = BrowserDriver(start_url=file_server)
        driver.configure_files([str(source)], tmp_path / "downloads")
        try:
            await driver.start()
        except RuntimeError as exc:
            if "Chromium" in str(exc):
                pytest.skip(str(exc))
            raise
        try:
            observation = await driver.observe()
            opener = next(element for element in observation["elements"] if element["text"] == "選擇附件")
            assert sum(element.get("input_type") == "file" for element in observation["elements"]) == 1
            await driver.execute(action("upload_file", target=opener["id"], text=str(source)))
            await driver._page.wait_for_function("document.querySelector('#hidden-uploaded').textContent.includes('自訂檔案選擇器測試')")
            assert await driver._page.locator("#hidden-uploaded").inner_text() == "附件.txt:自訂檔案選擇器測試"

            observation = await driver.observe()
            ordinary = next(element for element in observation["elements"] if element["text"] == "普通按鈕")
            # An unapproved path must fail before clicking even an observed target.
            outside = tmp_path / "not-authorized.txt"
            outside.write_text("not approved")
            with pytest.raises(ValueError, match="授權清單"):
                await driver.execute(action("upload_file", target=ordinary["id"], text=str(outside)))
            assert await driver._page.locator("#not-upload").inner_text() == "普通按鈕"
            with pytest.raises(ValueError, match="沒有開啟檔案選擇器"):
                await driver.execute(action("upload_file", target=ordinary["id"], text=str(source)))
            assert await driver._page.locator("#not-upload").inner_text() == "普通按鈕已點擊"
        finally:
            await driver.close()
    asyncio.run(run())


def test_close_cancels_incomplete_download_and_removes_partial_file(tmp_path):
    async def run():
        began = asyncio.Event()

        class SlowDownload:
            suggested_filename = "partial.txt"
            cancelled = False

            async def save_as(self, path):
                Path(path).write_text("partial bytes")
                began.set()
                await asyncio.sleep(30)

            async def cancel(self):
                self.cancelled = True

        driver = BrowserDriver()
        driver.configure_files([], tmp_path / "downloads")
        download = SlowDownload()
        driver._on_download(download)
        await began.wait()
        await driver.close()
        assert download.cancelled
        assert not driver.downloads and not driver.download_paths
        assert not list((tmp_path / "downloads").iterdir())
        assert not driver._download_tasks
    asyncio.run(run())
