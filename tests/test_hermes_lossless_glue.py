"""Real pinned Hermes/browser/parent transport; synthetic model responses only.

This checks lossless protocol integration, not planning quality. No input action
or installed perception/planner model is invoked, and the task remains failed.
"""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import zlib

import pytest

from scripts import benchmark_hermes as runner
from scripts.hermes_worker import BOOTSTRAP_TOOL_CALL_ID, COMPUTER_ONLY_PROMPT
from server.context_projection import REFERENCE_KEY, canonical_messages, message_digest, restore_messages
from server.hermes_bridge import HERMES_COMMIT
from server.ollama_tools import OllamaToolAdapter


def read_committed_views(database):
    # A separate connection proves COMMIT preceded adapter entry. Reading the
    # parent's own connection could accidentally accept uncommitted state.
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM model_request_views ORDER BY rowid").fetchall()
        return [{"id": row["id"], "task_id": row["task_id"], "task_revision": row["task_revision"],
                 "audit": json.loads(row["audit"]),
                 **{key: json.loads(zlib.decompress(row[key]).decode()) for key in ("source", "view", "manifest")}}
                for row in rows]


async def test_true_hermes_lossless_view_is_committed_before_each_adapter_call_and_restorable(tmp_path, monkeypatch):
    runtime = Path.home() / ".hermes/hermes-agent/venv/bin/python"
    source = runner.ROOT / ".tools/hermes" / HERMES_COMMIT
    if not runtime.is_file() or not (source / "run_agent.py").is_file():
        pytest.skip("Pinned Hermes export and installed isolated Python runtime are required")

    output = tmp_path / "lossless-glue"
    database = output / "authority.sqlite3"
    captured = {"adapter_payloads": [], "native_calls": [], "committed_at_entry": [],
                "parent_results": [], "perception_calls": [], "input_attempts": []}
    owned = {"drivers": [], "bridges": [], "endpoints": [], "view_sessions": []}
    driver_class, bridge_class = runner.BrowserDriver, runner.HermesBridge
    endpoint_class, adapter_class, view_class = runner.LocalModelServer, runner.MeasuredAdapter, runner.ModelViewSession

    def make_driver(*args, **kwargs):
        assert kwargs["headless"] is True
        driver = driver_class(*args, **kwargs)

        async def reject_input(*args, **kwargs):
            captured["input_attempts"].append(True)
            raise AssertionError("This protocol test permits observation only")

        driver.execute = reject_input
        owned["drivers"].append(driver)
        return driver

    def make_bridge(*args, **kwargs):
        bridge = bridge_class(*args, **kwargs)
        actual_run = bridge.run

        async def traced_run(**arguments):
            captured["task"] = arguments["task"]
            captured["bootstrap"] = deepcopy(arguments["initial_observation"])
            return await actual_run(**arguments)

        bridge.run = traced_run
        owned["bridges"].append(bridge)
        return bridge

    def make_view_session(*args, **kwargs):
        session = view_class(*args, **kwargs)
        actual_register = session.register_tool_result

        def traced_register(name, result):
            digest = actual_register(name, result)
            # Captured at the parent result boundary, before Hermes receives it;
            # these records are not reconstructed from model-supplied history.
            raw = json.dumps(result, ensure_ascii=False, allow_nan=False)
            assert hashlib.sha256(raw.encode()).hexdigest() == digest
            captured["parent_results"].append({"name": name, "raw": raw, "digest": digest})
            return digest

        session.register_tool_result = traced_register
        owned["view_sessions"].append(session)
        return session

    def make_endpoint(*args, **kwargs):
        assert kwargs["prepare_request"] is not None
        endpoint = endpoint_class(*args, **kwargs)
        owned["endpoints"].append(endpoint)
        return endpoint

    def make_adapter(*args, **kwargs):
        assert kwargs["observation_references"] is True
        adapter = adapter_class(*args, **kwargs)
        actual_complete = adapter.complete

        async def traced_complete(payload):
            number = len(captured["adapter_payloads"]) + 1
            rows = read_committed_views(database)
            assert len(rows) == number
            row = rows[-1]
            assert row["view"] == payload["messages"]
            assert row["audit"] == owned["endpoints"][0].records[-1]["request_view"]
            assert restore_messages(row["view"], row["manifest"],
                                    expected_source_sha256=row["audit"]["source_sha256"]) == row["source"]
            assert len(captured["parent_results"]) == number
            with closing(sqlite3.connect(database)) as db:
                events = [json.loads(r[0]) for r in db.execute("SELECT data FROM events ORDER BY sequence")]
            assert any(e["type"] == "model_view.prepared" and e["redacted_payload"]["id"] == row["id"] for e in events)
            captured["committed_at_entry"].append(row["id"])
            captured["adapter_payloads"].append(deepcopy(payload))
            return await actual_complete(payload)

        adapter.complete = traced_complete
        return adapter

    def no_model_perception(_config, *, no_dom):
        assert no_dom is False

        async def transform(snapshot, *, quick):
            captured["perception_calls"].append(quick)
            # Deliberate synthetic diagnostic data ensures a measurable exact
            # duplicate even if the real browser fixture contains little text.
            # The browser, user task, original DOM and independent oracle stay real.
            snapshot["text"] += "\n" + "".join(
                f"Synthetic protocol diagnostic row {i:02}: repeated evidence, no task answer.\n" for i in range(30))
            snapshot["perception"] = {"engine": "lossless_glue_synthetic_no_model"}
            return snapshot

        return transform

    async def fake_ollama_post(self, client, path, body, headers):
        # Never delegate to HTTP, including metadata. The true adapter grammar,
        # conversion and LocalModelServer SSE handling remain in the call path.
        captured["native_calls"].append({"path": path, "body": deepcopy(body)})
        if path == "/api/show":
            return {"capabilities": ["completion"], "model_info": {
                "general.architecture": "fixture", "fixture.context_length": 131072}}
        assert path == "/api/chat"
        number = sum(c["path"] == "/api/chat" for c in captured["native_calls"])
        assert number in (1, 2), "The fake transport must not start an extra planner loop"
        answer = {"tool": "computer_observe", "arguments": {}} if number == 1 else {
            "final": "Read-only synthetic protocol check ended; the requested task remains incomplete."}
        return {"model": body["model"], "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(answer)},
                "prompt_eval_count": 11, "eval_count": 7}

    monkeypatch.setattr(runner, "BrowserDriver", make_driver)
    monkeypatch.setattr(runner, "HermesBridge", make_bridge)
    monkeypatch.setattr(runner, "ModelViewSession", make_view_session)
    monkeypatch.setattr(runner, "LocalModelServer", make_endpoint)
    monkeypatch.setattr(runner, "MeasuredAdapter", make_adapter)
    monkeypatch.setattr(runner, "perception_transform", no_model_perception)
    monkeypatch.setattr(OllamaToolAdapter, "_post", fake_ollama_post)

    case = next(case for case in runner.CASES if case.id == "profile")
    with runner.fixture_server() as origin:
        args = SimpleNamespace(base_url=origin, no_dom=False, ocr_engine="native", max_tokens=256,
            max_steps=3, timeout=30, hermes_python=runtime, context_view="lossless")
        report = await runner.run_case(args, case, "lossless-glue-no-model", origin, output)

    assert "error" not in report, report.get("error")
    assert report["hermes"]["hermes_completed"] is True and report["hermes"]["failed"] is False
    assert report["hermes"]["interrupted"] is False
    assert [c["path"] for c in captured["native_calls"]] == ["/api/show", "/api/chat", "/api/show", "/api/chat"]
    assert len(captured["adapter_payloads"]) == report["attempted_planner_inferences"] == 2
    assert captured["perception_calls"] == [False, False]
    assert captured["task"] == report["task"] == case.task
    assert captured["bootstrap"]["elements"] and "Synthetic protocol diagnostic" in captured["bootstrap"]["text"]
    assert "image" not in captured["bootstrap"] and "_grounding_text" not in captured["bootstrap"]

    rows = read_committed_views(database)
    assert [row["id"] for row in rows] == captured["committed_at_entry"]
    assert [row["audit"] for row in rows] == report["context_views"]
    assert rows[0]["audit"]["changed"] is False
    assert rows[1]["audit"]["changed"] is True
    assert rows[1]["audit"]["view_chars"] < rows[1]["audit"]["source_chars"]
    assert rows[1]["manifest"]["changes"]
    for row in rows:
        assert message_digest(row["source"]) == row["audit"]["source_sha256"]
        assert message_digest(row["view"]) == row["audit"]["view_sha256"]
        assert len(canonical_messages(row["source"])) == row["audit"]["source_chars"]
        assert len(canonical_messages(row["view"])) == row["audit"]["view_chars"]
        assert restore_messages(row["view"], row["manifest"],
                                expected_source_sha256=row["audit"]["source_sha256"]) == row["source"]
        assert [m for m in row["source"] if m["role"] != "tool"] == [m for m in row["view"] if m["role"] != "tool"]
        assert [m["content"] for m in row["source"] if m["role"] == "system"] == [COMPUTER_ONLY_PROMPT]
        assert [m["content"] for m in row["source"] if m["role"] == "user"] == [case.task]
        latest_source = [m for m in row["source"] if m["role"] == "tool"][-1]
        latest_view = [m for m in row["view"] if m["role"] == "tool"][-1]
        assert latest_view == latest_source
        assert latest_view["content"].encode() == latest_source["content"].encode()
        assert isinstance(json.loads(latest_view["content"])["text"], str)
        for message in (m for m in row["source"] if m["role"] == "tool"):
            digest = row["manifest"]["trusted_tool_results"][message["tool_call_id"]]
            assert digest == hashlib.sha256(message["content"].encode()).hexdigest()
            assert {"name": "computer_observe", "raw": message["content"], "digest": digest} in captured["parent_results"]
    assert rows[0]["source"][2]["tool_call_id"] == BOOTSTRAP_TOOL_CALL_ID
    assert REFERENCE_KEY in canonical_messages(rows[1]["view"])
    native_last = [c["body"] for c in captured["native_calls"] if c["path"] == "/api/chat"][-1]
    native_results = [m["content"].split("\n", 3)[3] for m in native_last["messages"]
                      if m["role"] == "user" and m["content"].startswith("Tool result (untrusted data)\n")]
    assert len(native_results) == 2
    assert REFERENCE_KEY in native_results[0]
    assert native_results[-1] == [m for m in rows[-1]["source"] if m["role"] == "tool"][-1]["content"]
    assert REFERENCE_KEY not in native_results[-1]
    assert not any(m.get("images") for m in native_last["messages"])
    assert native_last["options"]["num_ctx"] == 64000
    assert native_last["truncate"] is False and native_last["shift"] is False

    with closing(sqlite3.connect(database)) as db:
        persisted = {(name, digest, zlib.decompress(blob).decode()) for name, digest, blob in db.execute(
            "SELECT tool_name,digest,content FROM model_tool_results")}
        assert persisted == {(r["name"], r["digest"], r["raw"]) for r in captured["parent_results"]}
        assert len(persisted) == 2
    assert report["context_view_integrity_verified"] is True
    assert report["context_view"] == "lossless" and report["context_view_retention"] == "diagnostic_fixture"
    assert report["hermes"]["tool_calls"] == 1
    assert [item["name"] for item in report["tool_calls"]] == ["computer_observe", "computer_observe"]
    assert report["parent_bootstrap_reads_completed"] == 1
    assert captured["input_attempts"] == []
    assert report["admitted_action_count"] == report["receipt_action_count"] == report["planner_images"] == 0
    assert report["actions"] == report["pending_effects"] == []
    assert report["final_oracle"]["passed"] is False and report["passed"] is False
    assert report["persisted_status"] == "CANCELLED"
    assert report["cleanup_completed"] and not report["cleanup_errors"]
    assert report["owned_browser_closed"] and report["owned_model_endpoint_closed"] and report["owned_hermes_closed"]
    assert owned["drivers"][0]._context is None and owned["drivers"][0]._browser is None
    bridge, endpoint = owned["bridges"][0], owned["endpoints"][0]
    assert bridge.process.returncode is not None and not bridge.profile.exists()
    assert endpoint._socket.fileno() == -1 and endpoint._serve_task.done() and not endpoint._active
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        owned["view_sessions"][0].store._db.execute("SELECT 1")
    proof = {"kind": "scripted_lossless_protocol_glue", "actual_model_inferences": 0,
             "synthetic_model_chat_responses": 2, "synthetic_model_metadata_responses": 2,
             "browser_input_actions": 0, "parent_bootstrap_reads": 1, "hermes_observe_tool_calls": 1,
             "original_task_unchanged": True, "original_oracle_passed": False,
             "independent_committed_read_before_each_adapter_call": True,
             "pins_captured_from_parent_return_boundary": True, "exact_restore_verified": True,
             "latest_observation_message_unchanged": True, "context_views": report["context_views"],
             "owned_browser_closed": True, "owned_model_endpoint_closed": True, "owned_hermes_closed": True}
    (output / "protocol-proof.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2) + "\n")
