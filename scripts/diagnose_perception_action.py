#!/usr/bin/env python3
"""One real OCR -> text planner -> browser click round, with independent oracle.

This is local synthetic tool conformance, NOT an autonomous task benchmark.
There is no DOM data or screenshot in the planner request. The browser is
headless; only a left click within this dedicated fixture may be executed.
"""
import argparse
import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
from pathlib import Path
import random
import re
import sys
import threading
import time
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from server.drivers import BrowserDriver
from server.perception import _local_ocr_url, enrich_observation
from server.providers import next_action
from server.schemas import ModelConfig

SOURCES = ["server/drivers.py", "server/perception.py", "server/providers.py", "server/action_space.py",
           "server/key_contract.py", "server/schemas.py", "server/browser_pointer.py", "server/motion.py",
           "scripts/diagnose_perception_action.py"]
SOURCE_HASHES = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


@contextmanager
def scene(seed):
    labels = ["Amber", "Jade"]
    random.Random(seed).shuffle(labels)
    markup = ("<!doctype html><meta charset=utf-8><title>Perception conformance</title>"
        "<style>body{margin:0;background:#faf8f4;color:#1f2937;font:28px Arial}h1{text-align:center;margin-top:70px;font-size:30px}"
        ".buttons{display:flex;justify-content:space-around;margin:150px 100px 70px}button{width:220px;height:90px;font:36px Arial;border:2px solid #333;background:white;border-radius:14px}"
        "#status{text-align:center;font-size:24px}</style><h1>Grounding fixture</h1><div class=buttons>"
        + "".join(f"<button>{label}</button>" for label in labels)
        + "</div><div id=status>Waiting</div><script>window.fixtureClickEvents=[];"
        "for(const button of document.querySelectorAll('button'))button.addEventListener('click',()=>{"
        "window.fixtureClickEvents.push(button.textContent);document.querySelector('#status').textContent='Selected '+button.textContent;});</script>").encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(markup)))
            self.end_headers()
            self.wfile.write(markup)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", labels
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ClickFixtureDriver(BrowserDriver):
    def __init__(self, origin):
        super().__init__(start_url=origin, headless=True)
        self.origin = origin

    async def _guard_request(self, route):
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            await route.abort("blockedbyclient")
            return
        await super()._guard_request(route)

    async def execute(self, action):
        if action.type != "click" or action.button != "left":
            raise ValueError("This one-round fixture only permits one left click")
        return await super().execute(action)


async def run_round(args, model, ocr_engine, seed):
    requested = "Jade"
    record = {"model": model, "ocr_engine": ocr_engine, "ocr_model": args.ocr_model if ocr_engine == "glm_ocr" else None,
        "planner_vision": False, "planner_dom": False, "seed": seed,
        "task": f"點一下「{requested}」按鈕。只做一次點擊。", "passed": False}
    began = time.monotonic()
    with scene(seed) as (origin, order):
        driver = ClickFixtureDriver(origin)
        record["left_to_right_labels"] = order
        try:
            async with asyncio.timeout(180):
                await driver.start()
                observation = await driver.observe()
                # Remove both the model's DOM view and its executable DOM handles.
                observation.update(target="browser", elements=[], text="", _grounding_text="")
                driver._handles = {}
                observation = await enrich_observation(observation, ocr_engine=ocr_engine,
                    ocr_model=args.ocr_model, ocr_base_url=args.base_url)
                driver.set_observation(observation)
                record["perception"] = observation.get("perception")
                record["ocr_elements"] = observation["elements"]
                filename = re.sub(r"[^A-Za-z0-9_-]", "_", model + "_" + ocr_engine + "_" + str(seed))
                picture = args.output.with_name(args.output.stem + "_" + filename + ".png")
                picture.write_bytes(base64.b64decode(observation["image"].split(",", 1)[1]))
                record["synthetic_screenshot"] = str(picture.resolve())
                config = ModelConfig(provider="ollama", base_url=args.base_url, model=model, vision=False,
                    perception="ocr", ocr_engine=ocr_engine, ocr_model=args.ocr_model, max_tokens=512)
                requests = []
                record["planner_requests"] = requests
                base_client = httpx.AsyncClient

                class RecordingClient(base_client):
                    async def post(self, url, **kwargs):
                        body = kwargs.get("json") or {}
                        request = None
                        if str(url).endswith("/api/chat") and body.get("model") == model:
                            messages = body.get("messages", [])
                            user = next((message.get("content", "") for message in messages if message.get("role") == "user"), "{}")
                            payload = json.loads(user)
                            view = payload.get("current_observation_UNTRUSTED", {})
                            request = {"endpoint": "/api/chat", "planner_images": sum(len(m.get("images", [])) for m in messages),
                                "dom_elements": sum(e.get("source") == "dom" for e in view.get("elements", [])),
                                "element_sources": sorted({e.get("source") for e in view.get("elements", [])}),
                                "contains_oracle": "fixtureClickEvents" in user,
                                "format": "schema" if isinstance(body.get("format"), dict) else body.get("format")}
                            requests.append(request)
                        response = await super().post(url, **kwargs)
                        if request is not None:
                            data = response.json()
                            message = data.get("message") or {}
                            request.update(http_status=response.status_code, final_content=(message.get("content") or "")[:4000],
                                thinking_chars=len(message.get("thinking") or ""), eval_count=data.get("eval_count"))
                        return response

                with patch("server.providers.httpx.AsyncClient", RecordingClient):
                    action = await next_action(config, record["task"], observation, [])
                record["action"] = action.model_dump(exclude_none=True, exclude_defaults=True)
                record["driver_result"] = await driver.execute(action)
                actual = await driver._page.evaluate("() => window.fixtureClickEvents")
                record["oracle"] = {"expected_clicks": [requested], "actual_clicks": actual,
                    "passed": actual == [requested]}
                record["passed"] = actual == [requested] and bool(requests) and all(not r["planner_images"] and not r["dom_elements"] and not r["contains_oracle"] for r in requests)
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            record["error"] = str(exc)[:1200]
        finally:
            if "oracle" not in record and driver._page is not None:
                try:
                    actual = await driver._page.evaluate("() => window.fixtureClickEvents")
                    record["oracle"] = {"expected_clicks": [requested], "actual_clicks": actual,
                        "passed": actual == [requested]}
                except Exception:
                    record["oracle"] = {"expected_clicks": [requested], "unavailable": True, "passed": False}
            await driver.close()
    record["seconds"] = round(time.monotonic() - began, 3)
    return record


async def main(args):
    args.base_url = _local_ocr_url(args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=15, trust_env=False, follow_redirects=False) as client:
        for model in set(args.model + ([args.ocr_model] if "glm_ocr" in args.ocr_engine else [])):
            response = await client.post(args.base_url + "/api/show", json={"model": model})
            response.raise_for_status()
            metadata = response.json()
            if metadata.get("remote_host") or metadata.get("remote_model") or "cloud" in model:
                raise RuntimeError("Only locally installed models are permitted")
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "scope": "Single tool-round grounding/input conformance, NOT autonomous task success",
        "provenance": {"source_sha256_at_import": SOURCE_HASHES}, "results": []}
    for model in args.model:
        for engine in args.ocr_engine:
            for seed in args.seed or [17]:
                result = await run_round(args, model, engine, seed)
                report["results"].append(result)
                report["passed"] = sum(item["passed"] for item in report["results"])
                report["total"] = len(report["results"])
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({key: result.get(key) for key in ("model", "ocr_engine", "seed", "passed", "action", "oracle", "error", "seconds")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--ocr-engine", action="append", choices=["native", "glm_ocr"], required=True)
    parser.add_argument("--ocr-model", default="glm-ocr:latest")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--seed", type=int, action="append", help="Repeat for multiple deterministic shuffled layouts (default: 17)")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
