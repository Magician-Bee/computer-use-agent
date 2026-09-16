#!/usr/bin/env python3
"""Actual Hermes -> policy/store/gateway -> headless browser integration.

The model endpoint is explicitly SCRIPTED. This proves plumbing and independent
fixture verification, not the intelligence of any model or a release gate.
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
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.core.contracts import (ActionEnvelope, CompletionVerdict, Evidence, ResourceScope,
                                   SuccessCriterion, Task)
from server.core.gateway import ActionGateway, browser_manifest
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import TaskStore
from server.drivers import BrowserDriver
from server.hermes_bridge import HermesBridge
from server.schemas import Action

HTML = '''<!doctype html><meta charset="utf-8"><title>Owned browser verification fixture</title>
<style>body{font:20px system-ui;padding:60px;background:#f8fafc}input{display:block;width:380px;padding:12px}button{margin-top:220px;margin-left:500px;padding:18px 36px}</style>
<label>Name<input id="name" aria-label="Name"></label><button id="save">Save</button><output id="result"></output>
<script>window.inputEvents=[];window.savedValue=null;window.saveCount=0;
for(const kind of ['pointermove','pointerdown','pointerup','click'])document.addEventListener(kind,e=>inputEvents.push({kind,x:e.clientX,y:e.clientY,t:performance.now()}),true);
save.onclick=()=>{savedValue=document.querySelector('input').value;saveCount++;result.textContent='Saved'};
</script>'''
VALUE = "代理自己的滑鼠與鍵盤"


async def diagnose(python: Path, output: Path):
    if output.exists():
        raise ValueError("Use a new evidence directory; existing outcomes must not be overwritten")
    output.mkdir(parents=True, mode=0o700)
    store = TaskStore(output / "authority.sqlite3")
    task = store.create_task(Task(id="hermes-browser-fixture", goal=f"Enter {VALUE} in Name and save it once.",
        policy_ref="fixture-authorized-input", success_criteria=(
            SuccessCriterion(id="exact-value", description="The saved value exactly matches the user's value", verifier_ref="fixture-readback"),
            SuccessCriterion(id="saved-once", description="Save was applied exactly once", verifier_ref="fixture-readback"))))
    for state in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, state)
    scope = ResourceScope(resource_id="fixture-browser", kind="browser", session_id="fixture-session")
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
                                      allowed_action_types=("type", "click"), approval_action_types=()))
    driver = BrowserDriver(headless=True)
    gateway = ActionGateway(store, policies)
    frame = None
    dispatched = []
    calls = []
    requests = []
    readback = None
    pointer_events = []
    result = None
    started = time.monotonic()
    bridge = HermesBridge(python)
    model_schema = [
        {"name": "computer_observe", "description": "Read the current agent browser through its parent gateway.",
         "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "computer_act", "description": "Propose one typed Action. Only the parent grants execution authority.",
         "parameters": {"type": "object", "properties": {"action": Action.model_json_schema()}, "required": ["action"], "additionalProperties": False}},
        {"name": "computer_finish", "description": "Request the independent verifier; a summary cannot grant success.",
         "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"], "additionalProperties": False}},
        {"name": "computer_ask_user", "description": "Ask through the parent task.",
         "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"], "additionalProperties": False}},
    ]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            raw = json.dumps({"data": [{"id": "scripted-integration-only", "context_length": 64000}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = len(requests)
            requests.append({"index": index, "tool_names": sorted(t["function"]["name"] for t in payload.get("tools", []))})
            name, args = None, None
            if index in (0, 2):
                name, args = "computer_observe", {}
            elif index in (1, 3):
                observation = json.loads(next(m["content"] for m in reversed(payload["messages"]) if m["role"] == "tool"))
                role = "textbox" if index == 1 else "button"
                target = next(item["id"] for item in observation["elements"] if item["role"] == role)
                action = {"type": "type" if index == 1 else "click", "target": target}
                if index == 1:
                    action["text"] = VALUE
                name, args = "computer_act", {"action": action}
            elif index == 4:
                name, args = "computer_finish", {"summary": "The scripted sequence requests independent verification."}
            delta = {"role": "assistant", "content": "Fixture sequence ended; use the parent's verdict."}
            finish = "stop"
            if name:
                delta = {"role": "assistant", "content": None, "tool_calls": [{"index": 0, "id": f"fixture_{index}",
                    "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
                finish = "tool_calls"
            common = {"id": f"integration_{index}", "object": "chat.completion.chunk", "created": int(time.time()), "model": "scripted-integration-only"}
            chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}]
            raw = ("".join("data: "+json.dumps(chunk)+"\n\n" for chunk in chunks)+"data: [DONE]\n\n").encode()
            self.send_response(200); self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    async def parent(name, arguments):
        nonlocal frame, task, readback, pointer_events
        calls.append(name)
        if name == "computer_observe" and arguments == {}:
            frame = await gateway.observe(task.id, task.revision, lease)
            return {"observation_id": frame.reference.id, "revision": frame.reference.revision,
                    "elements": frame.snapshot["elements"], "text": frame.snapshot["text"]}
        if name == "computer_act" and set(arguments) == {"action"}:
            action = Action.model_validate(arguments["action"])
            if frame is None:
                raise ValueError("Observe before proposing an action")
            identifier = uuid.uuid4().hex
            envelope = ActionEnvelope.create(action_id=identifier, task_id=task.id, task_revision=task.revision,
                step_id="step-"+identifier, tool_id="browser.input", tool_version="1.0", action=action,
                observation_id=frame.reference.id, observation_revision=frame.reference.revision,
                policy_ref=task.policy_ref, resource_scope=scope, lease_id=lease.id,
                fencing_token=lease.fencing_token, idempotency_key=identifier)
            result = await gateway.dispatch(envelope)
            dispatched.append(envelope)
            frame = None
            return result
        if name == "computer_finish" and set(arguments) == {"summary"}:
            task = store.transition(task.id, task.revision, "VERIFYING")
            verified_frame = await gateway.observe(task.id, task.revision, lease)
            # Kept out of model observations. This readback supplies the evidence;
            # model text and the gateway's successful transport receipts do not.
            readback = await driver._page.evaluate("({savedValue,saveCount,currentValue:document.querySelector('input').value})")
            pointer_events = await driver._page.evaluate("inputEvents")
            await driver._page.screenshot(path=str(output / "final-browser.png"))
            passed = readback == {"savedValue": VALUE, "saveCount": 1, "currentValue": VALUE}
            evidence = Evidence(id=uuid.uuid4().hex, task_id=task.id, task_revision=task.revision,
                observation_id=verified_frame.reference.id, observation_revision=verified_frame.reference.revision,
                source="external_verifier", observed_at=store.clock(), check_type="fixture-state-readback",
                result="pass" if passed else "fail", summary="Exact saved value and one committed save were independently read back",
                verifier_ref="fixture-readback", criterion_ids=("exact-value", "saved-once"))
            store.record_evidence(evidence)
            task = store.commit_verdict(task.id, task.revision, CompletionVerdict(verifier_ref="fixture-readback",
                verdict="succeeded" if passed else "failed", task_revision=task.revision,
                evidence_ids=(evidence.id,), criterion_ids=evidence.criterion_ids, decided_at=store.clock()))
            return {"verified": passed, "task_status": task.status}
        raise ValueError("Tool name or arguments are outside this parent fixture contract")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    report = {"kind": "scripted_hermes_browser_core_integration", "real_model_calls": 0, "passed": False}
    try:
        await driver.start()
        await driver._page.set_content(HTML)
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("type", "click")))
        lease = store.acquire_lease(task.id, scope, ttl_seconds=300)
        result = await bridge.run(task=task.goal, model="scripted-integration-only",
            base_url=f"http://127.0.0.1:{server.server_port}/v1", tools=model_schema, gateway=parent,
            cancel_gateway=lambda: gateway.stop(task.id, store.get_task(task.id).revision), max_iterations=10, timeout=60)
        # HermesBridge always drains its parent gateway on exit. Capture the
        # fixture before that cleanup, then prove the owned browser was closed.
        reopened = TaskStore(output / "authority.sqlite3")
        try:
            persisted = reopened.get_task(task.id)
            events = reopened.events(task.id)
            cached = reopened.existing_action_result(dispatched[-1]) if dispatched else None
        finally:
            reopened.close()
        moves = [event for event in pointer_events if event["kind"] == "pointermove"]
        report.update({"elapsed_seconds": round(time.monotonic()-started, 3), "requests": requests, "gateway_calls": calls,
            "hermes_result": result, "independent_readback": readback, "real_browser_actions": len(dispatched),
            "pointer_move_events": len(moves), "pointer_duration_ms": round(moves[-1]["t"]-moves[0]["t"], 2) if moves else 0,
            "persisted_status_after_reopen": persisted.status, "cached_action_result": cached,
            "event_count": len(events), "event_types": [event.type for event in events],
            "task_success_decided_by": "parent_fixture_readback", "physical_input_untouched": True,
            "owned_browser_closed_after_hermes": driver._context is None and driver._browser is None,
            "planner_images": 0, "oracle_fields_sent_to_planner": False})
        report["passed"] = (persisted.status == "SUCCEEDED" and len(dispatched) == 2 and len(moves) >= 15
            and result["hermes_completed"] is True and result["failed"] is False and result["completion_verified"] is False
            and len(requests) == 6 and cached is not None and cached["dispatch"] is False
            and report["owned_browser_closed_after_hermes"]
            and [event.sequence for event in events] == list(range(1, len(events)+1)))
    except Exception as exc:
        report.update(error_type=type(exc).__name__, error=str(exc), gateway_calls=calls, requests=requests,
                      hermes_result=result, independent_readback=readback, real_browser_actions=len(dispatched),
                      persisted_status_after_error=store.get_task(task.id).status)
    finally:
        await driver.close()
        await bridge.close()
        store.close()
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(diagnose(args.python, args.output)) else 1)
