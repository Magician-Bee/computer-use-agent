#!/usr/bin/env python3
"""Real Hermes loop against a scripted local API, with a fake parent gateway.

This checks IPC/tool scope; it is not AI intelligence, desktop or task evidence.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.hermes_bridge import HermesBridge


async def diagnose(python: Path, output: Path, fail_last: bool = False):
    requests = []
    gateway_calls = []
    canary = "CUA_DIAGNOSTIC_PRIVATE_MARKER_b1d57"
    sequence = [("terminal", {"command": "THIS_MUST_NEVER_EXECUTE"}),
                ("computer_observe", {}),
                ("computer_act", {"action": {"type": "click", "target": "fixture_button"}}),
                ("computer_finish", {"summary": "Synthetic protocol fixture changed"})]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def send(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path.endswith("/models"):
                self.send({"data": [{"id": "scripted-protocol-only", "context_length": 64000}]})
            else:
                self.send({"error": "Unsupported diagnostic route"}, 404)
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
            if not self.path.endswith("/chat/completions"):
                return self.send({"error": "Unsupported diagnostic route"}, 404)
            index = len(requests)
            requests.append({"tool_names": sorted(t["function"]["name"] for t in payload.get("tools", [])),
                "model": payload.get("model"), "stream": payload.get("stream"),
                "message_roles": [m.get("role") for m in payload.get("messages", [])]})
            if fail_last and index >= len(sequence):
                return self.send({"error": {"message": "Synthetic final request failure", "type": "invalid_request_error"}}, 400)
            message = {"role": "assistant", "content": "Protocol fixture finished; task completion remains unverified."}
            finish_reason = "stop"
            if index < len(sequence):
                name, arguments = sequence[index]
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": f"probe_{index}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
                finish_reason = "tool_calls"
            if payload.get("stream"):
                delta = dict(message)
                for position, call in enumerate(delta.get("tool_calls", [])):
                    call["index"] = position
                common = {"id": f"diagnostic_{index}", "object": "chat.completion.chunk",
                          "created": int(time.time()), "model": "scripted-protocol-only"}
                chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                          {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]}]
                raw = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send({"id": f"diagnostic_{index}", "object": "chat.completion", "created": int(time.time()),
                "model": "scripted-protocol-only", "choices": [{"index": 0, "message": message,
                "finish_reason": finish_reason}], "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}})

    tools = [
        {"name": "computer_observe", "description": "Get a fresh observation from the parent gateway.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "computer_act", "description": "Request one action through the parent gateway.",
         "parameters": {"type": "object", "properties": {"action": {"type": "object"}}, "required": ["action"]}},
        {"name": "computer_finish", "description": "Request independent verification; does not certify completion.",
         "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}},
        {"name": "computer_ask_user", "description": "Ask the user through the parent task.",
         "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    ]
    async def gateway(name, arguments):
        gateway_calls.append({"name": name, "arguments": arguments})
        if name == "computer_observe":
            return {"fixture": True, "elements": [{"id": "fixture_button", "text": "Protocol button"}]}
        if name == "computer_finish":
            return {"verified": False, "reason": "This is only an IPC protocol diagnostic."}
        return {"fixture": True, "action_executed_on_computer": False}

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    started = time.monotonic()
    bridge = HermesBridge(python)
    try:
        result = await bridge.run(task="Exercise the four available parent gateway tools for a protocol diagnostic. " + canary,
            model="scripted-protocol-only", base_url=f"http://127.0.0.1:{server.server_port}/v1",
            tools=tools, gateway=gateway, max_iterations=8, timeout=90, retain_profile=True)
        dumps = list(bridge.profile.rglob("request_dump_*.json"))
        canary_persisted = any(canary.encode() in path.read_bytes() for path in bridge.profile.rglob("*") if path.is_file())
        outcome_expected = (result["failed"] is True and result["hermes_completed"] is False) if fail_last else (
            result["failed"] is False and result["hermes_completed"] is True)
        passed = ([call["name"] for call in gateway_calls] == ["computer_observe", "computer_act", "computer_finish"]
            and result["completion_verified"] is False and len(requests) == 5
            and outcome_expected and result["interrupted"] is False and not dumps and not canary_persisted)
        report = {"kind": "scripted_model_protocol_conformance", "passed": passed, "real_model_calls": 0,
            "real_computer_actions": 0, "elapsed_seconds": round(time.monotonic()-started, 3),
            "expected_hermes_failure": fail_last, "request_dump_files": len(dumps), "canary_persisted": canary_persisted,
            "requests": requests, "gateway_calls": gateway_calls, "result": result}
    except Exception as exc:
        report = {"kind": "scripted_model_protocol_conformance", "passed": False, "real_model_calls": 0,
            "real_computer_actions": 0, "error": str(exc), "profile": str(bridge.profile),
            "requests": requests, "gateway_calls": gateway_calls}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "hermes-protocol-conformance.json")
    parser.add_argument("--fail-last", action="store_true", help="Inject a final HTTP 400; success means it is reported as failure without request dumps")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(diagnose(args.python, args.output, args.fail_last)) else 1)
