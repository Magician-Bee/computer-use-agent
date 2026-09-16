#!/usr/bin/env python3
"""Real local models -> pinned Hermes -> core gateway -> unchanged task oracles.

Each case owns a headless Chromium process, SQLite store and authenticated model
endpoint. No scripted actions/fallbacks. Artifacts contain synthetic fixture data
only; this diagnostic transcript capture is not a production retention policy.
"""
from __future__ import annotations

import argparse
import asyncio
import copy
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from benchmarks.cases import CASES, grade
from benchmarks.core_specs import core_spec
from benchmarks.provenance import provenance
from server.core.computer_tools import ComputerTools, perception_transform
from server.core.contracts import Budget, ResourceScope, Task
from server.core.gateway import ActionGateway, browser_manifest
from server.core.model_views import ModelViewSession
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import TaskStore
from server.core.verifiers import VerifierRegistry
from server.drivers import BrowserDriver
from server.hermes_bridge import HermesBridge, HERMES_COMMIT, HERMES_TREE_SHA256
from server.model_server import LocalModelServer
from server.ollama_tools import OllamaToolAdapter
from server.schemas import ModelConfig

CODE_PROVENANCE = provenance(ROOT)
ACTIONS = ("navigate", "click", "double_click", "move", "drag", "type", "key", "scroll", "wait",
           "back", "forward", "select_option", "new_tab", "switch_tab", "close_tab")


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@contextmanager
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(ROOT / "benchmarks/fixtures")))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class MeasuredAdapter(OllamaToolAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.inferences = []

    async def _post(self, client, path, body, headers):
        if path != "/api/chat":
            return await super()._post(client, path, body, headers)
        record = {"model": body["model"], "options": copy.deepcopy(body["options"]),
                  "truncate": body.get("truncate"), "shift": body.get("shift"),
                  "messages": copy.deepcopy(body["messages"]), "format": copy.deepcopy(body["format"]),
                  "status": "started", "started_at": datetime.now(timezone.utc).isoformat()}
        self.inferences.append(record)
        call_number = len(self.inferences)
        started = time.monotonic()
        try:
            result = await super()._post(client, path, body, headers)
            record.update(status="returned", response={key: result[key] for key in
                ("model", "message", "done", "done_reason", "prompt_eval_count", "eval_count", "total_duration",
                 "prompt_eval_duration", "eval_duration", "load_duration") if key in result})
            # Keep only public final-channel content in synthetic diagnostics.
            record["response"]["message"] = {key: value for key, value in result.get("message", {}).items()
                                              if key != "thinking"}
            return result
        except BaseException as exc:
            record.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                          error_type=type(exc).__name__)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic()-started, 3)
            print(json.dumps({"model_call": call_number, "model": body["model"],
                "status": record["status"], "seconds": record["elapsed_seconds"]}), flush=True)


def inference_accounting(inferences):
    """Include failed/incomplete calls' returned usage; missing counts are unknown."""
    known = [record.get("response", {}) for record in inferences]
    known = [response for response in known if all(type(response.get(key)) is int and response[key] >= 0
                                                 for key in ("prompt_eval_count", "eval_count"))]
    return {"attempted_calls": len(inferences), "calls_with_usage": len(known),
        "calls_with_unknown_usage": len(inferences)-len(known),
        "known_prompt_tokens": sum(response["prompt_eval_count"] for response in known),
        "known_completion_tokens": sum(response["eval_count"] for response in known),
        "usage_complete": len(known) == len(inferences),
        "planner_images": sum(len(message.get("images", [])) for record in inferences
                              for message in record.get("messages", []))}


def benchmark_passed(report):
    """A model summary, timeout, partial cleanup or source drift never passes."""
    hermes = report.get("hermes", {})
    return (report.get("persisted_status") == "SUCCEEDED" and report.get("final_oracle", {}).get("passed") is True
        and not report.get("error") and not report.get("readback_error") and not report.get("cleanup_errors")
        and hermes.get("hermes_completed") is True and hermes.get("failed") is False
        and hermes.get("interrupted") is False and report.get("attempted_planner_inferences", 0) > 0
        and report.get("cleanup_completed") is True and report.get("owned_browser_closed") is True
        and report.get("owned_model_endpoint_closed") is True and report.get("owned_hermes_closed") is True
        and report.get("source_provenance_unchanged") is True
        and (report.get("context_view", "full") == "full" or (
            report.get("context_view") == "lossless"
            and report.get("context_view_integrity_verified") is True
            and len(report.get("context_views", [])) == report.get("attempted_planner_inferences"))))


async def run_case(args, case, model, origin, output):
    started = time.monotonic()
    deadline = started + args.timeout
    output.mkdir(parents=True, exist_ok=False)
    spec = core_spec(case)
    config = ModelConfig(provider="ollama", model=model, base_url=args.base_url, vision=False,
        perception="ocr", ocr_engine=args.ocr_engine,
        ocr_model="glm-ocr:latest", ocr_base_url=args.base_url, max_tokens=args.max_tokens)
    store = TaskStore(output / "authority.sqlite3")
    identifier = uuid.uuid4().hex
    task = store.create_task(Task(id=identifier, goal=case.task, policy_ref="benchmark-"+identifier,
        success_criteria=spec.success_criteria, budget=Budget(max_steps=args.max_steps, max_seconds=args.timeout)))
    for state in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, state)
    scope = ResourceScope(resource_id="browser-"+identifier, kind="browser", session_id=identifier, website_origin=origin)
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
        allowed_action_types=ACTIONS, approval_action_types=()))
    driver = BrowserDriver(start_url=origin+case.path, headless=True)
    driver.configure_allowed_origin(origin)
    transform = perception_transform(config, no_dom=args.no_dom)
    perception_calls = []
    async def measured_perception(snapshot, *, quick):
        began = time.monotonic()
        record = {"quick": quick, "status": "started"}
        perception_calls.append(record)
        try:
            result = await transform(snapshot, quick=quick)
            record.update(status="returned", perception={key: value for key, value in result.get("perception", {}).items()
                if key != "transcript"}, element_count=len(result.get("elements", [])))
            return result
        except BaseException as exc:
            record.update(status="interrupted" if isinstance(exc, asyncio.CancelledError) else "failed",
                          error_type=type(exc).__name__)
            raise
        finally:
            record["seconds"] = round(time.monotonic()-began, 3)
    gateway = ActionGateway(store, policies, observation_transform=measured_perception)
    registry = VerifierRegistry()
    registry.register_callback(task, spec.make_callback(), verifier_id=spec.verifier_id)
    facade = ComputerTools(store, gateway, registry, task_id=task.id, scope=scope, driver=driver, allowed_action_types=ACTIONS)
    context_mode = getattr(args, "context_view", "full")
    if context_mode not in {"full", "lossless"}:
        raise ValueError("Unknown context view mode")
    context_views = ModelViewSession(store, task.id, retention_mode="diagnostic_fixture") if context_mode == "lossless" else None
    adapter = MeasuredAdapter(config, context_length=64000, tools_provider=facade.definitions,
                              observation_references=context_views is not None)
    endpoint = LocalModelServer(adapter, model=model, context_length=64000,
                                prepare_request=context_views.prepare_request if context_views else None)
    bridge = HermesBridge(args.hermes_python)
    report = {"model": model, "case": case.id, "task": case.task, "passed": False,
        "planner_vision": False, "planner_images": 0, "no_dom": args.no_dom,
        "ocr_engine": args.ocr_engine, "ocr_model": config.ocr_model, "requested_context_length": 64000,
        "physical_input_untouched": True, "hermes_commit": HERMES_COMMIT, "hermes_tree_sha256": HERMES_TREE_SHA256,
        "production_ui_cutover": False, "fixture_oracle_unchanged": True, "scripted_planner_calls": 0,
        "context_view": context_mode,
        "context_view_retention": "diagnostic_fixture" if context_views else None,
        "started_at": datetime.now(timezone.utc).isoformat(), "origin": origin}
    report["provenance_import"] = CODE_PROVENANCE
    report["provenance_before_case"] = provenance(ROOT)
    report["parent_bootstrap_read_attempts"] = 0
    report["parent_bootstrap_reads_completed"] = 0
    report["environment_feedback"] = "initial_read_and_post_action_observation"
    report["case_timeout_seconds"] = args.timeout
    report["execution_phase"] = "setup"
    registered = False

    cleanup_task = None

    async def cleanup():
        nonlocal cleanup_task
        if cleanup_task is None:
            cleanup_task = asyncio.create_task(cleanup_owned_browser())
        await asyncio.shield(cleanup_task)

    async def cleanup_owned_browser():
        # Read the independent final oracle before releasing this owned browser.
        # This never supplies missing answers/actions to Hermes or grants success.
        if driver._context is not None:
            try:
                async with asyncio.timeout(15):
                    snapshot = {"storage": await driver._context.storage_state(), "pages": [
                        {"url": p.url, "path": urlsplit(p.url).path} for p in driver._context.pages if not p.is_closed()]}
                    report["final_oracle"] = grade(case.id, snapshot)
                    report["final_checks"] = [item.model_dump(mode="json") for item in await registry.verify(task.id, driver)]
                    if driver._page is not None:
                        await driver._page.screenshot(path=str(output / "final-browser.png"))
                    (output / "fixture-state.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2)+"\n")
            except Exception as exc:
                report["readback_error"] = type(exc).__name__
        report["status_before_cleanup"] = store.get_task(task.id).status
        if registered:
            await gateway.stop(task.id, store.get_task(task.id).revision)
        else:
            await driver.close()
        report["cleanup_completed"] = True

    async def tool(name, arguments):
        result = await facade(name, arguments)
        if context_views:
            context_views.register_tool_result(name, result)
        observed = result.get("observation", result if "observation_id" in result else {})
        observation_metadata = {key: observed[key] for key in ("observation_id", "observation_revision")
                                if isinstance(observed, dict) and key in observed}
        print(json.dumps({"tool": name, "actions": len(facade.actions), "status": store.get_task(task.id).status,
                          "error": result.get("error"), "observation_error": result.get("observation_error"),
                          "observation": observation_metadata, "verified": result.get("verified")}), flush=True)
        return result

    try:
        # Initial environment sensing is real parent I/O, counted against the
        # same case deadline. It supplies no scripted action or hidden answer.
        async with asyncio.timeout_at(deadline):
            report["execution_phase"] = "browser_start"
            await driver.start()
            gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ACTIONS))
            registered = True
            report["execution_phase"] = "initial_observation"
            report["parent_bootstrap_read_attempts"] = 1
            initial_observation = await facade("computer_observe", {})
            if "observation_id" not in initial_observation:
                raise RuntimeError("Initial parent observation was not available")
            report["parent_bootstrap_reads_completed"] = 1
            if context_views:
                context_views.register_tool_result("computer_observe", initial_observation)
            report["execution_phase"] = "hermes_conversation"
            async with endpoint:
                report["hermes"] = await bridge.run(task=case.task, model=model, base_url=endpoint.url,
                    tools=facade.definitions(), gateway=tool, cancel_gateway=cleanup,
                    initial_observation=initial_observation,
                    max_iterations=args.max_steps*3+12, timeout=max(.001, deadline-time.monotonic()),
                    max_tokens=args.max_tokens, context_length=64000, model_gateway_token=endpoint.token)
            report["execution_phase"] = "conversation_returned"
    except BaseException as exc:
        report["failed_phase"] = report["execution_phase"]
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
    finally:
        # An earlier cleanup error must not skip the remaining owned resources
        # or suppress the failure artifact. Each phase is bounded and audited.
        report["cleanup_errors"] = []
        cleanup_interrupt = None
        for phase, close in (("hermes", bridge.close), ("gateway", cleanup),
                             ("model_endpoint", endpoint.close), ("browser", driver.close)):
            try:
                await asyncio.wait_for(close(), timeout=20)
            except BaseException as exc:
                report["cleanup_errors"].append({"phase": phase, "type": type(exc).__name__})
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                    cleanup_interrupt = exc
        report.update(elapsed_seconds=round(time.monotonic()-started, 3),
            persisted_status=store.get_task(task.id).status, tool_calls=facade.calls,
            actions=[item.action.model_dump(exclude_none=True, exclude_defaults=True) for item in facade.actions],
            verifications=facade.verifications, model_requests=endpoint.records,
            attempted_planner_inferences=len(adapter.inferences), perception_calls=perception_calls,
            owned_browser_closed=driver._context is None and driver._browser is None,
            owned_model_endpoint_closed=(endpoint._socket is None or endpoint._socket.fileno() == -1)
                and (endpoint._serve_task is None or endpoint._serve_task.done()) and not endpoint._active,
            owned_hermes_closed=(bridge.process is None or bridge.process.returncode is not None)
                and (bridge._launch is None or bridge._launch.done()),
            events=[item.model_dump(mode="json") for item in store.events(task.id)])
        report["actions_semantics"] = "Actions with returned receipts; admitted operations may have unknown outcomes."
        report["receipt_action_count"] = len(facade.actions)
        report["admitted_action_count"] = sum(item["type"] == "action.dispatched" for item in report["events"])
        report["pending_effects"] = store.pending_effects(task.id)
        report["unresolved_action_count"] = len(report["pending_effects"])
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
                report["ollama_loaded_after_case"] = (await client.get(args.base_url.rstrip("/")+"/api/ps")).json()
        except Exception as exc:
            report["ollama_ps_error"] = type(exc).__name__
        report["planner_context_verified_after_case"] = any(
            (item.get("name") == model or item.get("model") == model) and item.get("context_length") == 64000
            for item in report.get("ollama_loaded_after_case", {}).get("models", []) if isinstance(item, dict))
        report["planner_usage"] = inference_accounting(adapter.inferences)
        report["planner_images"] = report["planner_usage"]["planner_images"]
        report["context_views"] = [item["request_view"] for item in endpoint.records if "request_view" in item]
        # Reopen/readback integrity is checked against the original retained
        # messages, not the model's ability to recite its own context.
        report["context_view_integrity_verified"] = None
        if context_views:
            try:
                readback_store = TaskStore(output / "authority.sqlite3")
                try:
                    readback_views = ModelViewSession(readback_store, task.id, retention_mode="diagnostic_fixture")
                    for audit in report["context_views"]:
                        if readback_views.read_view(audit["id"])["audit"] != audit:
                            raise RuntimeError("Context view audit mismatch")
                finally:
                    readback_store.close()
                report["context_view_integrity_verified"] = True
            except Exception as exc:
                report["context_view_integrity_verified"] = False
                report["context_view_error"] = type(exc).__name__
        # A transform invocation is not proof that GLM reached /api/chat. Its
        # internal recognizer transport has no per-request hook in this runner.
        report["perception_inference_accounting"] = {"transform_invocations": len(perception_calls),
            "glm_transforms_requested": sum(not item["quick"] and args.ocr_engine == "glm_ocr" for item in perception_calls),
            "glm_transforms_returned": sum(not item["quick"] and item["status"] == "returned"
                and args.ocr_engine == "glm_ocr" for item in perception_calls),
            "exact_model_request_count": None, "token_usage": None,
            "limitation": "Perception attempts and token usage are not instrumented at the model transport."}
        report["provenance_after_case"] = provenance(ROOT)
        report["source_provenance_unchanged"] = (CODE_PROVENANCE["source_sha256"]
            == report["provenance_before_case"]["source_sha256"] == report["provenance_after_case"]["source_sha256"])
        report["passed"] = benchmark_passed(report)
        store.close()
        (output / "inferences.json").write_text(json.dumps(adapter.inferences, ensure_ascii=False, indent=2)+"\n")
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
        if cleanup_interrupt is not None:
            raise cleanup_interrupt
    print(json.dumps({"case": case.id, "model": model, "passed": report["passed"],
                      "actions": len(report["actions"]), "seconds": report["elapsed_seconds"]}), flush=True)
    return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", action="append", choices=("qwen3-vl:2b", "minicpm-v4.6:latest"))
    parser.add_argument("--case", action="append", choices=[case.id for case in CASES])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--hermes-python", type=Path, default=Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434")
    parser.add_argument("--ocr-engine", choices=("glm_ocr", "native"), default="glm_ocr")
    parser.add_argument("--no-dom", action="store_true")
    parser.add_argument("--context-view", choices=("full", "lossless"), default="full",
                        help="Keep full history or persist a reversible observation-reference view; scored separately")
    parser.add_argument("--max-steps", type=int, default=24)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--timeout", type=float, default=420)
    args = parser.parse_args()
    if not 1 <= args.max_steps <= 100 or not 30 <= args.timeout <= 3600:
        parser.error("Use 1..100 steps and 30..3600 seconds")
    args.output.mkdir(parents=True, exist_ok=False)
    results = []
    with fixture_server() as origin:
        for model in args.model or ["qwen3-vl:2b", "minicpm-v4.6:latest"]:
            for case in CASES:
                if args.case and case.id not in args.case:
                    continue
                output = args.output / (model.replace(":", "-")+"-"+case.id)
                results.append(await run_case(args, case, model, origin, output))
                summary = {"kind": "real_hermes_core_local_model_benchmark", "provenance": CODE_PROVENANCE,
                    "passed": sum(item["passed"] for item in results), "total": len(results),
                    "results": [{key: item.get(key) for key in ("model", "case", "passed", "error", "persisted_status",
                        "context_view", "attempted_planner_inferences", "elapsed_seconds", "final_oracle")} for item in results]}
                (args.output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
    return 0 if all(item["passed"] for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
