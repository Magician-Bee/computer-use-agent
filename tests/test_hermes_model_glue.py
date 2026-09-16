"""Real runner/worker/browser bootstrap, synthetic model HTTP responses only.

No installed model is invoked, no action is scripted, and no native/headed
surface is used. This is protocol glue coverage, never model-task success.
"""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import benchmark_hermes as runner
from scripts.hermes_worker import BOOTSTRAP_TOOL_CALL_ID, COMPUTER_ONLY_PROMPT
from server.hermes_bridge import HERMES_COMMIT
from server.ollama_tools import OllamaToolAdapter


async def test_real_runner_bootstrap_reaches_pinned_hermes_first_request_and_closes_every_resource(tmp_path, monkeypatch):
    runtime = Path.home() / ".hermes/hermes-agent/venv/bin/python"
    source = runner.ROOT / ".tools/hermes" / HERMES_COMMIT
    if not runtime.is_file() or not (source / "run_agent.py").is_file():
        pytest.skip("Pinned Hermes export and installed isolated Python runtime are required")

    captured = {"model_payloads": [], "ollama_paths": [], "perception_calls": []}
    owned = {"drivers": [], "bridges": [], "endpoints": []}
    driver_class, bridge_class = runner.BrowserDriver, runner.HermesBridge
    endpoint_class, adapter_class = runner.LocalModelServer, runner.MeasuredAdapter

    def make_driver(*args, **kwargs):
        assert kwargs["headless"] is True
        driver = driver_class(*args, **kwargs)
        owned["drivers"].append(driver)
        return driver

    def make_bridge(*args, **kwargs):
        bridge = bridge_class(*args, **kwargs)
        actual_run = bridge.run

        async def traced_run(**arguments):
            captured["task"] = arguments["task"]
            captured["initial_observation"] = copy.deepcopy(arguments["initial_observation"])
            return await actual_run(**arguments)

        bridge.run = traced_run
        owned["bridges"].append(bridge)
        return bridge

    def make_endpoint(*args, **kwargs):
        endpoint = endpoint_class(*args, **kwargs)
        owned["endpoints"].append(endpoint)
        return endpoint

    def make_adapter(*args, **kwargs):
        adapter = adapter_class(*args, **kwargs)
        actual_complete = adapter.complete

        async def traced_complete(payload):
            # Capture the true worker's HTTP payload before the real adapter
            # converts historical tool messages for the Ollama wire protocol.
            captured["model_payloads"].append(copy.deepcopy(payload))
            return await actual_complete(payload)

        adapter.complete = traced_complete
        return adapter

    def no_model_perception(_config, *, no_dom):
        assert not no_dom

        async def transform(snapshot, *, quick):
            captured["perception_calls"].append(quick)
            snapshot["perception"] = {"engine": "glue_fixture_no_recognition_model"}
            return snapshot

        return transform

    async def synthetic_ollama_post(self, client, path, body, headers):
        # This is the only replaced model transport. Never delegate to httpx,
        # even for metadata: no real Ollama or external endpoint is contacted.
        captured["ollama_paths"].append(path)
        if path == "/api/show":
            return {"capabilities": ["completion"], "model_info": {
                "general.architecture": "fixture", "fixture.context_length": 131072}}
        assert path == "/api/chat"
        return {"model": body["model"], "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps({
                "final": "Read-only synthetic protocol check ended. The requested browser task remains incomplete."})},
            "prompt_eval_count": 11, "eval_count": 7}

    monkeypatch.setattr(runner, "BrowserDriver", make_driver)
    monkeypatch.setattr(runner, "HermesBridge", make_bridge)
    monkeypatch.setattr(runner, "LocalModelServer", make_endpoint)
    monkeypatch.setattr(runner, "MeasuredAdapter", make_adapter)
    monkeypatch.setattr(runner, "perception_transform", no_model_perception)
    monkeypatch.setattr(OllamaToolAdapter, "_post", synthetic_ollama_post)

    case = next(case for case in runner.CASES if case.id == "profile")
    with runner.fixture_server() as origin:
        # /api/ps is a harmless 404 from this owned static fixture server; the
        # adapter's /api/show/chat calls above never make network connections.
        args = SimpleNamespace(base_url=origin, no_dom=False, ocr_engine="native", max_tokens=256,
            max_steps=3, timeout=30, hermes_python=runtime)
        report = await runner.run_case(args, case, "glue-fixture-no-model", origin, tmp_path / "glue")

    assert "error" not in report, report.get("error")
    assert report["hermes"]["hermes_completed"] is True
    assert report["hermes"]["failed"] is False and report["hermes"]["interrupted"] is False
    assert captured["ollama_paths"] == ["/api/show", "/api/chat"]
    assert len(captured["model_payloads"]) == 1
    assert captured["perception_calls"] == [False]
    assert captured["task"] == report["task"] == case.task

    initial = captured["initial_observation"]
    assert initial["observation_id"] and initial["observation_revision"] == 1
    assert initial["elements"] and any(item["source"] == "dom" for item in initial["elements"])
    assert "image" not in initial and "_grounding_text" not in initial
    messages = captured["model_payloads"][0]["messages"]
    systems = [message for message in messages if message["role"] == "system"]
    assert len(systems) == 1 and systems[0]["content"] == COMPUTER_ONLY_PROMPT
    prompt_metadata = report["hermes"]["ready"]["system_prompt"]
    assert prompt_metadata["chars"] == len(COMPUTER_ONLY_PROMPT)
    assert prompt_metadata["sha256"] == hashlib.sha256(COMPUTER_ONLY_PROMPT.encode()).hexdigest()
    user_messages = [message for message in messages if message["role"] == "user"]
    assert user_messages[-1]["content"] == case.task  # no task concatenation
    calls = [call for message in messages if message["role"] == "assistant"
             for call in message.get("tool_calls", []) if call["id"] == BOOTSTRAP_TOOL_CALL_ID]
    assert len(calls) == 1 and calls[0]["function"]["name"] == "computer_observe"
    assert json.loads(calls[0]["function"]["arguments"]) == {}
    results = [message for message in messages if message["role"] == "tool"
               and message.get("tool_call_id") == BOOTSTRAP_TOOL_CALL_ID]
    assert len(results) == 1 and json.loads(results[0]["content"]) == initial
    assert all(initial["observation_id"] not in str(message.get("content", ""))
               for message in messages if message["role"] == "system")

    assert report["parent_bootstrap_read_attempts"] == report["parent_bootstrap_reads_completed"] == 1
    assert report["hermes"]["parent_bootstrap"]["read_count"] == 1
    assert report["hermes"]["parent_bootstrap"]["model_calls"] == 0
    assert report["hermes"]["tool_calls"] == 0
    assert [item["name"] for item in report["tool_calls"]] == ["computer_observe"]
    assert report["admitted_action_count"] == report["receipt_action_count"] == 0
    assert report["actions"] == report["pending_effects"] == []
    assert report["final_oracle"]["passed"] is False and report["passed"] is False
    assert report["persisted_status"] == "CANCELLED"
    assert report["cleanup_completed"] and not report["cleanup_errors"]
    assert report["owned_browser_closed"] and report["owned_model_endpoint_closed"] and report["owned_hermes_closed"]
    assert owned["drivers"][0]._context is None and owned["drivers"][0]._browser is None
    bridge, endpoint = owned["bridges"][0], owned["endpoints"][0]
    assert bridge.process is not None and bridge.process.returncode is not None
    assert bridge.profile is not None and not bridge.profile.exists()
    assert endpoint._socket.fileno() == -1 and endpoint._serve_task.done() and not endpoint._active
    assert report["source_provenance_unchanged"] is True
    proof = {"kind": "scripted_protocol_glue_smoke", "actual_model_inferences": 0,
        "synthetic_model_http_responses": 1, "browser_actions": 0, "original_oracle_passed": False,
        "first_request_system_prompt": prompt_metadata, "source_provenance_unchanged": True,
        "parent_bootstrap_reads": 1, "original_task_unchanged": True,
        "bootstrap_matches_true_facade": True, "bootstrap_is_system_instruction": False,
        "owned_browser_closed": True, "owned_model_endpoint_closed": True, "owned_hermes_closed": True}
    (tmp_path / "glue/protocol-proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2)+"\n")
    (tmp_path / "glue/first-hermes-request.json").write_text(
        json.dumps(captured["model_payloads"][0], ensure_ascii=False, indent=2)+"\n")
