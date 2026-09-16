"""Real headless, loopback-only regression fixtures for child-frame prose."""
import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from server.browser_text import collect_page_text
from server.drivers import BrowserDriver


@pytest.fixture(scope="module")
def frame_site():
    origins = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/visible":
                body = '''<h2>訂單摘要</h2><p>取件碼：VISIBLE-FRAME-4729</p>
                <button>確認取件</button><iframe src="/nested" style="width:550px;height:80px"></iframe>'''
            elif url.path == "/nested":
                body = '<p>領取地點：NESTED-COUNTER-816</p>'
            elif url.path == "/secret":
                body = '<p>HIDDEN-FRAME-SECRET-9137</p><iframe src="/secret-nested"></iframe>'
            elif url.path == "/secret-nested":
                body = '<p>HIDDEN-NESTED-SECRET-6201</p>'
            else:
                mode = parse_qs(url.query).get("hidden", ["display"])[0]
                styles = {
                    "display": "display:none", "visibility": "visibility:hidden",
                    "opacity": "opacity:0", "offscreen": "position:absolute;top:1800px",
                    "ancestor": "", "clipped": "",
                }
                wrapper = {"ancestor": "display:none", "clipped": "height:0;overflow:hidden"}.get(mode, "")
                body = f'''<h1>請讀取訂單取件碼與領取地點</h1>
                  <iframe name="visible-order" src="{origins['child']}/visible" style="width:700px;height:260px"></iframe>
                  <div style="{wrapper}"><iframe name="secret-order" src="{origins['child']}/secret"
                    style="width:500px;height:160px;{styles[mode]}"></iframe></div>'''
            html = ('<!doctype html><meta charset="utf-8"><style>body{margin:8px;font:16px sans-serif}</style>' + body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    origins["child"] = f"http://127.0.0.1:{servers[1].server_port}"
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    try:
        yield f"http://127.0.0.1:{servers[0].server_port}"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


async def open_fixture(url):
    pytest.importorskip("playwright.async_api")
    driver = BrowserDriver(url, headless=True)
    try:
        await driver.start()
    except RuntimeError as exc:
        if "Chromium" in str(exc):
            pytest.skip(str(exc))
        raise
    await driver._page.wait_for_load_state("load")
    return driver


def test_observation_includes_visible_cross_origin_frame_task_prose(frame_site):
    async def run():
        driver = await open_fixture(frame_site)
        try:
            observed = await driver.observe()
            # Controls already worked; the missing static prose made a text-only
            # LLM unable to answer the actual task despite seeing the button.
            assert any(element["text"] == "確認取件" for element in observed["elements"])
            assert "VISIBLE-FRAME-4729" in observed["text"]
            assert "NESTED-COUNTER-816" in observed["text"]
        finally:
            await driver.close()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["display", "visibility", "opacity", "offscreen", "ancestor", "clipped"])
def test_frame_text_helper_excludes_hidden_frames_and_their_descendants(frame_site, mode):
    async def run():
        driver = await open_fixture(f"{frame_site}/?hidden={mode}")
        try:
            text = await collect_page_text(driver._page)
            assert "VISIBLE-FRAME-4729" in text
            assert "NESTED-COUNTER-816" in text
            assert "HIDDEN-FRAME-SECRET-9137" not in text
            assert "HIDDEN-NESTED-SECRET-6201" not in text
            assert len(await collect_page_text(driver._page, limit=80)) <= 80
            assert await collect_page_text(driver._page, limit=0) == ""
        finally:
            await driver.close()
    asyncio.run(run())
