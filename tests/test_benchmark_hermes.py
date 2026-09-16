"""Runner control tests with real owned headless HTTP, zero model inference.

The fixture-no-inference adapter returns synthetic JSON. Positive scoring here
tests reporting logic only; these temporary artifacts are never model results.
"""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from scripts import benchmark_hermes as runner
from server.ollama_tools import OllamaToolAdapter


def successful_report():
    return {"persisted_status": "SUCCEEDED", "final_oracle": {"passed": True},
        "hermes": {"hermes_completed": True, "failed": False, "interrupted": False},
        "attempted_planner_inferences": 1, "cleanup_completed": True,
        "owned_browser_closed": True, "owned_model_endpoint_closed": True,
        "owned_hermes_closed": True, "source_provenance_unchanged": True}


@pytest.mark.parametrize("field,value", [("cleanup_completed", False), ("owned_browser_closed", False),
    ("owned_model_endpoint_closed", False), ("owned_hermes_closed", False),
    ("source_provenance_unchanged", False), ("attempted_planner_inferences", 0),
    ("error", {"type": "TimeoutError"}), ("readback_error", "TimeoutError"),
    ("cleanup_errors", [{"phase": "gateway", "type": "RuntimeError"}])])
def test_success_gate_requires_complete_cleanup_provenance_and_an_inference(field, value):
    report = successful_report()
    assert runner.benchmark_passed(report)
    report[field] = value
    assert not runner.benchmark_passed(report)


def test_lossless_success_requires_verified_view_for_every_inference():
    report = successful_report()
    report["context_view"] = "lossless"
    assert not runner.benchmark_passed(report)
    report["context_view_integrity_verified"] = True
    assert not runner.benchmark_passed(report)
    report["context_views"] = [{"id": "persisted-fixture-view"}]
    assert runner.benchmark_passed(report)
    report["context_view_integrity_verified"] = False
    assert not runner.benchmark_passed(report)


def test_usage_counts_every_returned_attempt_and_labels_missing_usage_unknown():
    usage = runner.inference_accounting([
        {"status": "returned", "response": {"prompt_eval_count": 10, "eval_count": 3}},
        # Invalid final JSON still consumed tokens: don't count only accepted tools.
        {"status": "returned", "response": {"prompt_eval_count": 20, "eval_count": 4}},
        {"status": "interrupted"}])
    assert usage == {"attempted_calls": 3, "calls_with_usage": 2, "calls_with_unknown_usage": 1,
        "known_prompt_tokens": 30, "known_completion_tokens": 7, "usage_complete": False, "planner_images": 0}


@pytest.mark.parametrize("mode", ["returned", "timeout", "interrupted", "close_error", "supplier_unfinished"])
async def test_runner_uses_original_oracle_dynamic_schema_and_drains_on_every_outcome(tmp_path, monkeypatch, mode):
    drivers, configs, captured = [], [], {}
    original_driver = runner.BrowserDriver

    class FixtureDriver(original_driver):
        async def start(self):
            drivers.append(self)
            await super().start()
            if mode != "supplier_unfinished":
                # Test-only committed state to exercise the true original
                # verifier's SUCCEEDED branch without pretending to be a model.
                await self._page.evaluate("""localStorage.setItem('bench_profile',JSON.stringify({
                    name:'林沛安',email:'pei.an@example.test',department:'design',seat:'window',snack:true,submitted:true}))""")

    def fake_perception(config, *, no_dom):
        configs.append(config)

        async def transform(snapshot, *, quick):
            snapshot["perception"] = {"engine": "synthetic_test_no_model"}
            return snapshot

        return transform

    async def fake_ollama_post(self, client, path, body, headers):
        if path == "/api/show":
            return {"capabilities": ["completion"], "model_info": {
                "general.architecture": "fixture", "fixture.context_length": 131072}}
        assert path == "/api/chat"
        captured["actual_format"] = copy.deepcopy(body["format"])
        return {"model": body["model"], "done": True, "done_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps({"final": "Synthetic control test"}),
                        "thinking": "SHOULD-NOT-BE-LOGGED"}, "prompt_eval_count": 31, "eval_count": 7}

    class FakeBridge:
        def __init__(self, *_):
            self.process = self._launch = None
            self.arguments = None

        async def run(self, **kwargs):
            self.arguments = kwargs
            captured["task"] = kwargs["task"]
            captured["initial_observation"] = kwargs["initial_observation"]
            # Exercise the actual transport boundary even when conversation
            # and inference are synthetic. A real browser view must fit it.
            from scripts.hermes_worker import validate_initial_observation
            assert validate_initial_observation(captured["initial_observation"]) == captured["initial_observation"]
            assert "observation_id" in captured["initial_observation"]
            assert "image" not in captured["initial_observation"]
            assert 0 < kwargs["timeout"] < 30  # bootstrap uses the same deadline
            observed = await kwargs["gateway"]("computer_observe", {})
            captured["observed"] = observed
            # Real owned model HTTP endpoint -> real adapter with only its
            # Ollama transport replaced. The supplied initial schema is stale;
            # the actual grammar must be rebuilt from the current observation.
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(kwargs["base_url"] + "/chat/completions",
                    headers={"Authorization": "Bearer " + kwargs["model_gateway_token"]},
                    json={"model": kwargs["model"], "messages": [{"role": "user", "content": kwargs["task"]}],
                          "tools": [{"type": "function", "function": item} for item in kwargs["tools"]]})
                assert response.status_code == 200
            captured["finish"] = await kwargs["gateway"]("computer_finish", {"summary": "I claim everything is finished"})
            if mode == "timeout":
                await self.close()
                raise TimeoutError("Synthetic deadline after verified state")
            await self.close()
            return {"hermes_completed": True, "failed": False, "interrupted": mode == "interrupted"}

        async def close(self):
            if mode == "close_error":
                raise RuntimeError("Synthetic bridge cleanup failure")
            if self.arguments:
                await self.arguments["cancel_gateway"]()

    monkeypatch.setattr(runner, "BrowserDriver", FixtureDriver)
    monkeypatch.setattr(runner, "HermesBridge", FakeBridge)
    monkeypatch.setattr(runner, "perception_transform", fake_perception)
    monkeypatch.setattr(OllamaToolAdapter, "_post", fake_ollama_post)
    with runner.fixture_server() as origin:
        args = SimpleNamespace(base_url=origin, no_dom=False, ocr_engine="native", max_tokens=256,
            max_steps=5, timeout=30, hermes_python=Path("unused-synthetic-runtime"))
        case = next(item for item in runner.CASES if item.id == ("supplier" if mode == "supplier_unfinished" else "profile"))
        output = tmp_path / mode
        report = await runner.run_case(args, case, "fixture-no-inference", origin, output)
    assert configs[0].perception == "ocr"
    assert captured["task"] == case.task
    assert report["owned_browser_closed"] and report["owned_model_endpoint_closed"]
    assert drivers[0]._context is None and drivers[0]._browser is None
    assert json.loads((output / "report.json").read_text())["passed"] == report["passed"]
    assert report["attempted_planner_inferences"] == 1
    assert report["parent_bootstrap_read_attempts"] == report["parent_bootstrap_reads_completed"] == 1
    assert report["planner_usage"]["known_prompt_tokens"] == 31
    assert report["planner_usage"]["known_completion_tokens"] == 7
    assert report["planner_images"] == 0
    grammar = json.dumps(captured["actual_format"])
    assert any(item["id"] in grammar for item in captured["observed"]["elements"])
    inference = json.loads((output / "inferences.json").read_text())[0]
    assert inference["format"] == captured["actual_format"]
    assert inference["truncate"] is False and inference["shift"] is False
    assert "SHOULD-NOT-BE-LOGGED" not in json.dumps(inference)
    assert set(report["provenance_before_case"]["source_sha256"]) == set(runner.CODE_PROVENANCE["source_sha256"])
    if mode == "returned":
        assert report["execution_phase"] == "conversation_returned"
        assert report["final_oracle"]["passed"] and captured["finish"]["verified"]
        assert report["passed"]
    else:
        assert not report["passed"]
    if mode == "close_error":
        assert report["cleanup_errors"] == [{"phase": "hermes", "type": "RuntimeError"}]
        assert report["cleanup_completed"]
    if mode == "timeout":
        assert report["persisted_status"] == "SUCCEEDED" and report["final_oracle"]["passed"]
        assert report["error"]["type"] == "TimeoutError"
    if mode == "supplier_unfinished":
        visible_payload = json.dumps({"task": captured["task"], "observed": captured["observed"],
                                      "finish": captured["finish"]}, ensure_ascii=False)
        assert "GH-742" not in visible_payload and "supply@starlake.example.test" not in visible_payload
        assert captured["finish"]["verified"] is False
        assert not report["final_oracle"]["passed"]


async def test_bootstrap_perception_uses_case_deadline_and_never_starts_inference(tmp_path, monkeypatch):
    entered, cancelled = asyncio.Event(), asyncio.Event()

    def slow_perception(_config, *, no_dom):
        async def transform(snapshot, *, quick):
            entered.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return snapshot
        return transform

    async def forbidden_completion(*_args, **_kwargs):
        raise AssertionError("No model may run before a successful bootstrap")

    monkeypatch.setattr(runner, "perception_transform", slow_perception)
    monkeypatch.setattr(runner.MeasuredAdapter, "complete", forbidden_completion)
    with runner.fixture_server() as origin:
        args = SimpleNamespace(base_url=origin, no_dom=False, ocr_engine="native", max_tokens=256,
            max_steps=5, timeout=2, hermes_python=Path("unused-synthetic-runtime"))
        report = await runner.run_case(args, runner.CASES[0], "fixture-no-inference", origin, tmp_path / "bootstrap-timeout")
    assert entered.is_set() and cancelled.is_set()
    assert report["error"]["type"] == "TimeoutError"
    assert report["failed_phase"] == "initial_observation"
    assert not report["passed"]
    assert report["parent_bootstrap_read_attempts"] == 1
    assert report["parent_bootstrap_reads_completed"] == report["attempted_planner_inferences"] == 0
    assert report["owned_browser_closed"] and report["owned_model_endpoint_closed"] and report["owned_hermes_closed"]
    assert report["persisted_status"] == "CANCELLED"
