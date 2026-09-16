#!/usr/bin/env python3
"""Run real configured models against local tasks and independent state oracles.

No scripted planner or success fallback exists here. The production Run loop
performs every action. Only browser creation and final-state capture are wrapped.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks.cases import CASES, grade
from benchmarks.provenance import provenance
from server.agent import Run
from server.drivers import BrowserDriver
from server.providers import next_action as production_next_action
from server.schemas import ModelConfig, RunRequest

# Capture when these modules are imported; later source edits must not be
# attributed to already-loaded Python code in a multi-case benchmark process.
CODE_PROVENANCE = provenance(ROOT)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


@contextmanager
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "benchmarks" / "fixtures")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class CapturingBrowserDriver(BrowserDriver):
    def __init__(self, *, start_url, origin, no_dom, result):
        super().__init__(start_url=start_url)
        self.origin = origin
        self.no_dom = no_dom
        self.result = result

    async def _guard_request(self, route):
        parsed = urlsplit(route.request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != self.origin:
            await route.abort("blockedbyclient")
            return
        await super()._guard_request(route)

    async def observe(self):
        observation = await super().observe()
        if self.no_dom:
            observation["elements"] = []
            observation["text"] = "\n".join(f"[{tab['id']}] {'目前分頁' if tab['active'] else '分頁'}: {tab['title']} {tab['url']}" for tab in observation["tabs"])
            self._handles = {}
            self.set_observation(observation)
        return observation

    async def close(self):
        if self._context is not None:
            try:
                self.result["storage"] = await self._context.storage_state()
                self.result["pages"] = [
                    {"url": page.url, "path": urlsplit(page.url).path}
                    for page in self._context.pages if not page.is_closed()
                ]
            except Exception as exc:
                self.result["capture_error"] = type(exc).__name__
        await super().close()


class BenchmarkRun(Run):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observation_timings = []

    async def observe(self, **kwargs):
        began = time.monotonic()
        try:
            return await super().observe(**kwargs)
        finally:
            self.observation_timings.append(round(time.monotonic() - began, 3))

    def emit(self, kind, message, action=None):
        super().emit(kind, message, action)
        if kind in {"action", "error", "done"}:
            print(f"  step {self.step:02d} {kind}: {message[:180]}", flush=True)
        if self.status == "awaiting_input":
            raise RuntimeError("Benchmark requires human input: " + (self.pending_input or "unspecified"))


async def run_case(args, case, model, origin):
    captured, planning = {}, []
    config = ModelConfig(provider="ollama", base_url=args.base_url, model=model,
        vision=args.vision, perception="ocr", ocr_engine=args.ocr_engine,
        ocr_model=args.ocr_model, ocr_base_url=args.base_url, max_tokens=args.max_tokens)
    request = RunRequest(task=case.task, target="browser", approval_mode="auto",
        max_steps=args.max_steps, start_url=origin + case.path)
    run = BenchmarkRun(request, config, demo_url="")

    async def timed_planner(*planner_args, **planner_kwargs):
        began = time.monotonic()
        record = {"started_at": datetime.now(timezone.utc).isoformat()}
        try:
            action = await production_next_action(*planner_args, **planner_kwargs)
            record["action_type"] = action.type
            return action
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            raise
        finally:
            record["seconds"] = round(time.monotonic() - began, 3)
            planning.append(record)

    factory = lambda start_url: CapturingBrowserDriver(start_url=start_url, origin=origin, no_dom=args.no_dom, result=captured)
    started = datetime.now(timezone.utc).isoformat()
    began = time.monotonic()
    timed_out = False
    code_state = CODE_PROVENANCE
    print(f"\n[{model}] {case.id}: {case.description}", flush=True)
    with patch("server.agent.BrowserDriver", factory), patch("server.agent.next_action", timed_planner):
        run.task = asyncio.create_task(run.execute_loop())
        try:
            await asyncio.wait_for(asyncio.shield(run.task), timeout=args.timeout)
        except TimeoutError:
            timed_out = True
            await run.stop()
    oracle = grade(case.id, captured)
    # A timeout must not pass even if its unfinished UI happened to match.
    passed = oracle["passed"] and not timed_out and "capture_error" not in captured
    result = {
        "model": model, "case": case.id, "task": case.task, "started_at": started,
        "seconds": round(time.monotonic() - began, 3), "passed": passed,
        "status": run.status, "timed_out": timed_out, "steps": run.step,
        "model_claimed_completed": run.status == "completed",
        "false_completion": run.status == "completed" and not oracle["passed"],
        "provenance": code_state,
        "planner_screenshots_enabled": args.vision,
        "observation_mode": "ocr_only" if args.no_dom else "dom_plus_ocr",
        "ocr_engine": args.ocr_engine, "ocr_model": args.ocr_model,
        "oracle": oracle, "final_fixture_state": captured, "error": run.error,
        "actions": run.history, "events": run.events,
        "planning_timings": planning, "observation_seconds": run.observation_timings,
    }
    print(f"  {'PASS' if passed else 'FAIL'} · {run.status} · {run.step} steps · {result['seconds']} s", flush=True)
    return result


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", help="Ollama model; repeat for multiple models")
    parser.add_argument("--case", action="append", choices=[case.id for case in CASES], help="Case to run; repeat to select several")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--vision", action="store_true", help="Send screenshots to planner (default: text only)")
    parser.add_argument("--no-dom", action="store_true", help="Remove browser DOM text/targets; use OCR targets plus tab metadata")
    parser.add_argument("--ocr-engine", choices=["native", "glm_ocr"], default="native")
    parser.add_argument("--ocr-model", default="glm-ocr:latest")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--repeat", type=int, choices=range(1, 21), default=1, metavar="1..20", help="Independent runs per model/case, each with a new browser context")
    parser.add_argument("--timeout", type=float, default=420, help="Seconds allowed per case")
    parser.add_argument("--output", type=Path, help="JSON report path")
    parser.add_argument("--list", action="store_true", help="List task cases without calling a model")
    return parser.parse_args()


async def main(args):
    selected = [case for case in CASES if not args.case or case.id in args.case]
    if args.list:
        for case in selected:
            print(f"{case.id}: {case.description}\n  {case.task}")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "benchmarks" / "results" / f"models-{stamp}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    with fixture_server() as origin:
        for model in args.model or ["minicpm-v4.6:latest", "qwen3-vl:2b"]:
            for case in selected:
                for repetition in range(1, args.repeat + 1):
                    result = await run_case(args, case, model, origin)
                    result["repetition"] = repetition
                    results.append(result)
                    report = {"created_at": stamp, "scope": "isolated local browser tasks, not a universal desktop benchmark", "results": results,
                        "passed": sum(result["passed"] for result in results), "total": len(results)}
                    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nSaved {output} · {sum(result['passed'] for result in results)}/{len(results)} passed", flush=True)
    return 0 if all(result["passed"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
