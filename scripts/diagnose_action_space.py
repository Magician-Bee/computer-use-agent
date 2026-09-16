#!/usr/bin/env python3
"""Explicit local-only action schema conformance on a synthetic text observation."""
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
from jsonschema import Draft202012Validator
from server.action_space import build_action_space, prepare_model_observation
from server.perception import _local_ocr_url
from server.providers import _apply_ollama_compatibility, build_request, observation_text, parse_action, ProviderError
from server.schemas import ModelConfig


async def main(args):
    base = _local_ocr_url(args.base_url)
    observation = prepare_model_observation({"target": "browser", "width": 800, "height": 600,
        "title": "Synthetic local settings fixture", "text": "Project settings. Not saved.",
        "elements": [
            {"id": "e_name", "source": "dom", "role": "textbox", "text": "Project name", "value": "Untitled", "x": 160, "y": 100, "width": 200, "height": 40},
            {"id": "e_color", "source": "dom", "role": "combobox", "text": "Color", "value": "blue", "options": [{"label": "Blue", "value": "blue"}, {"label": "Red", "value": "red"}], "x": 160, "y": 200, "width": 200, "height": 40},
            {"id": "e_notify", "source": "dom", "role": "checkbox", "text": "Enable notifications", "checked": False, "x": 160, "y": 300, "width": 24, "height": 24},
            {"id": "e_save", "source": "dom", "role": "button", "text": "Save project", "x": 160, "y": 400, "width": 140, "height": 40},
        ]})
    schema = build_action_space(observation)
    validator = Draft202012Validator(schema)
    prompt = observation_text("Rename the project to Orchard Review, choose Red, enable notifications, then save.", observation, [])
    report = {"tested_at": datetime.now(timezone.utc).isoformat(), "scope": "Synthetic text only; no GUI input, no images, no cloud inference", "schema": schema, "results": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=115, trust_env=False, follow_redirects=False) as client:
        for model in args.model:
            metadata = (await client.post(base + "/api/show", json={"model": model})).json()
            if metadata.get("remote_host") or metadata.get("remote_model") or "cloud" in model:
                raise RuntimeError("Only locally installed models are allowed")
            config = ModelConfig(provider="ollama", base_url=base, model=model, vision=False, max_tokens=512)
            url, headers, body = build_request(config, prompt, action_schema=schema)
            await _apply_ollama_compatibility(client, url, headers, body)
            record = {"model": model, "format": "discriminated_affordance_schema", "max_tokens": 512, "num_ctx": 16384,
                      "think": False, "assistant_final_prefill": body["messages"][-1]["role"] == "assistant"}
            began = time.monotonic()
            try:
                async with asyncio.timeout(120):
                    response = await client.post(url, headers=headers, json=body)
                data = response.json()
                message = data.get("message") or {}
                content = message.get("content") or ""
                record.update({"http_status": response.status_code, "done_reason": data.get("done_reason"),
                    "eval_count": data.get("eval_count"), "thinking_chars": len(message.get("thinking") or ""), "final_content": content[:4000]})
                if response.status_code >= 300:
                    record["error"] = data.get("error")
                try:
                    action = parse_action(content)
                    record["action"] = action.model_dump(exclude_none=True, exclude_defaults=True)
                    record["schema_valid"] = validator.is_valid(json.loads(content))
                except (ProviderError, ValueError):
                    record["schema_valid"] = False
            except (httpx.HTTPError, TimeoutError, ValueError, AttributeError) as exc:
                record["error_type"] = type(exc).__name__
            record["seconds"] = round(time.monotonic() - began, 3)
            report["results"].append(record)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
