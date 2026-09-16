#!/usr/bin/env python3
"""Explicit local, synthetic prompt diagnostics; records final content only."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from server.perception import _local_ocr_url
from server.providers import SYSTEM, observation_text, parse_action, ProviderError
from server.schemas import Action


async def main(args):
    base = _local_ocr_url(args.base_url)
    observation = {"target": "browser", "width": 1280, "height": 800, "title": "Synthetic settings fixture", "url": "http://127.0.0.1:8765/synthetic-only",
        "text": "Project settings. Project name: Untitled. Notifications disabled. Not saved.", "elements": [
            {"id": "e1", "role": "textbox", "text": "Project name", "value": "Untitled", "x": 300, "y": 200, "width": 200, "height": 40},
            {"id": "e2", "role": "checkbox", "text": "Enable notifications", "checked": False, "x": 150, "y": 300, "width": 24, "height": 24},
            {"id": "e3", "role": "button", "text": "Save project", "x": 550, "y": 400, "width": 140, "height": 40}]}
    prompt = observation_text("Rename the project to Orchard Review, enable notifications and save.", observation, [])
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
    native = {"model": args.model, "messages": messages, "stream": False, "think": False, "options": {"num_ctx": 16384, "num_predict": args.max_tokens, "temperature": 0}, "keep_alive": "5m"}
    compatible = {"model": args.model, "messages": messages, "stream": False, "max_tokens": args.max_tokens, "temperature": 0}
    raw_prefix = "<|im_start|>system\n" + SYSTEM + "<|im_end|>\n<|im_start|>user\n" + prompt + "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    raw = {"model": args.model, "prompt": raw_prefix, "raw": True, "stream": False, "think": False, "format": "json", "options": native["options"], "keep_alive": "5m"}
    variants = [
        ("raw_closed_think_json", "/api/generate", raw),
        ("raw_no_think_json", "/api/generate", {**raw, "prompt": raw_prefix.replace("<think>\n\n</think>\n\n", "")}),
        ("native_prefill_closed_think_json", "/api/chat", {**native, "format": "json", "messages": [*messages, {"role": "assistant", "content": "<think>\n\n</think>\n\n"}]}),
        ("native_prompt_close_think", "/api/chat", {**native, "messages": [*messages, {"role": "user", "content": "End your thinking block immediately and provide the final action JSON now."}]}),
        ("native_json", "/api/chat", {**native, "format": "json"}),
        ("native_no_format", "/api/chat", native),
        ("native_schema", "/api/chat", {**native, "format": Action.model_json_schema()}),
        ("native_think_enabled_no_format", "/api/chat", {**native, "think": True}),
        ("compatible_no_format", "/v1/chat/completions", compatible),
        ("compatible_reasoning_none", "/v1/chat/completions", {**compatible, "reasoning_effort": "none"}),
    ]
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "model": args.model, "scope": "Synthetic text fixture only; no computer actions or real screen data", "results": []}
    async with httpx.AsyncClient(timeout=115, trust_env=False, follow_redirects=False) as client:
        metadata = (await client.post(base + "/api/show", json={"model": args.model})).json()
        if metadata.get("remote_host") or metadata.get("remote_model") or "cloud" in args.model:
            raise RuntimeError("Only a locally installed model is allowed")
        report["capabilities"] = metadata.get("capabilities")
        report["template"] = metadata.get("template")
        for name, endpoint, body in variants:
            if args.variant and name not in args.variant:
                continue
            began = time.monotonic()
            record = {"variant": name, "endpoint": endpoint, "format": "schema" if isinstance(body.get("format"), dict) else body.get("format"), "think": body.get("think"), "reasoning_effort": body.get("reasoning_effort"), "max_tokens": args.max_tokens, "num_ctx": body.get("options", {}).get("num_ctx"), "assistant_final_prefill": bool(body.get("messages") and body["messages"][-1].get("role") == "assistant")}
            try:
                async with asyncio.timeout(120):
                    response = await client.post(base + endpoint, json=body)
                record["http_status"] = response.status_code
                data = response.json()
                if endpoint == "/api/chat":
                    message = data.get("message") or {}
                    final = message.get("content") or ""
                    record.update({"done": data.get("done"), "done_reason": data.get("done_reason"), "eval_count": data.get("eval_count"), "prompt_eval_count": data.get("prompt_eval_count"), "thinking_chars": len(message.get("thinking") or "")})
                elif endpoint == "/api/generate":
                    final = data.get("response") or ""
                    record.update({"done": data.get("done"), "done_reason": data.get("done_reason"), "eval_count": data.get("eval_count"), "prompt_eval_count": data.get("prompt_eval_count"), "thinking_chars": len(data.get("thinking") or "")})
                else:
                    choice = (data.get("choices") or [{}])[0]
                    message = choice.get("message") or {}
                    final = message.get("content") or ""
                    record.update({"finish_reason": choice.get("finish_reason"), "usage": data.get("usage"), "thinking_chars": len(message.get("reasoning") or message.get("reasoning_content") or "")})
                record["final_content"] = final[:4000]
                try:
                    record["valid_action"] = parse_action(final).model_dump(exclude_none=True, exclude_defaults=True)
                except ProviderError:
                    record["valid_action"] = None
            except (httpx.HTTPError, TimeoutError, ValueError, AttributeError) as exc:
                record["error_type"] = type(exc).__name__
            record["seconds"] = round(time.monotonic() - began, 3)
            report["results"].append(record)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--variant", action="append")
    parser.add_argument("--max-tokens", type=int, default=1024)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
