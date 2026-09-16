"""Lossless parent retention: temporary SQLite and real loopback HTTP only.

All messages and adapter responses are synthetic. These tests run no model,
browser, native input, or production service and do not establish a stage gate.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import random
import sqlite3
import string
import zlib

import httpx
import pytest
import pytest_asyncio

import server.core.model_views as model_views
from server.context_projection import canonical_messages, restore_messages
from server.core.contracts import SuccessCriterion, Task
from server.core.model_views import ModelViewSession
from server.core.store import AdmissionDenied, Conflict, StoreError, TaskStore
from server.model_server import LocalModelServer


MODE = "diagnostic_fixture"
MODEL = "synthetic-fixture-model"
CANARY = "synthetic-private-history-不可出現在安全稽核"


def task_at(store, identifier="task-main"):
    task = store.create_task(Task(id=identifier, goal="Read the synthetic fixture", policy_ref="fixture-policy",
        success_criteria=(SuccessCriterion(id="saved", description="Synthetic save oracle is true"),)))
    for state in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, state)
    return task


@pytest.fixture
def world(tmp_path):
    path = tmp_path / "sole-task-store.sqlite3"
    opened = []

    def open_store():
        store = TaskStore(path)
        opened.append(store)
        return store

    store = open_store()
    task = task_at(store)
    yield store, task, open_store, path
    for current in opened:
        current.close()


def session_for(store, task, **kwargs):
    return ModelViewSession(store, task.id, retention_mode=MODE, **kwargs)


def observation(revision):
    return {"observation_id": f"observation-{revision}", "observation_revision": revision, "target": "browser",
        "url": "https://fixture.invalid/profile", "title": "Synthetic profile",
        "text": CANARY + "\n" + ('中文 🙂 exact \\ bytes\tand \\"quotes\\"\n' * 90),
        "elements": [{"id": "save", "label": "儲存", "description": "visible label " * 60}],
        "tabs": [{"id": "tab-1", "url": "https://fixture.invalid/profile", "title": "Fixture " * 70}]}


def history(*, count=3, name="computer_observe"):
    messages = [{"role": "system", "content": "Only the original synthetic user task is an instruction."},
        {"role": "user", "content": CANARY + "\n原始任務：保留所有字元 🙂"}]
    results = []
    for revision in range(1, count + 1):
        result = observation(revision)
        if name != "computer_observe":
            result = {"ok": False, "error": "synthetic_readback", "observation": result}
        results.append(result)
        messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [{"id": f"call-{revision}",
                "type": "function", "function": {"name": name, "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": f"call-{revision}", "name": name,
                "content": json.dumps(result, ensure_ascii=False, allow_nan=False)}])
    return messages, results


def payload(messages):
    return {"model": MODEL, "messages": messages, "stream": False, "temperature": 0,
        "tools": [{"type": "function", "function": {"name": "computer_observe", "parameters": {
            "type": "object", "properties": {}}}}], "tool_choice": "auto"}


def pin_all(session, results, name="computer_observe"):
    for result in results:
        session.register_tool_result(name, result)


def count_rows(store, table, task_id="task-main"):
    assert table in {"model_request_views", "model_tool_results", "model_view_retention"}
    return store._db.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id=?", (task_id,)).fetchone()[0]


def safe_events(store, task_id="task-main"):
    return [event for event in store.events(task_id) if event.type.startswith("model_view.")]


class FakeAdapter:
    def __init__(self, on_call=None):
        self.calls = []
        self.on_call = on_call

    async def complete(self, request):
        if self.on_call:
            self.on_call(request)
        self.calls.append(deepcopy(request))
        return {"id": "synthetic-completion", "object": "chat.completion", "created": 0, "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Synthetic reply"},
                         "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


@pytest_asyncio.fixture
async def endpoint():
    instances = []

    async def create(session, adapter=None):
        server = LocalModelServer(adapter or FakeAdapter(), model=MODEL, context_length=64000,
                                  prepare_request=session.prepare_request)
        instances.append(server)
        await server.__aenter__()
        return server

    yield create
    for server in reversed(instances):
        await server.close()


async def post(server, request):
    async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=5,
            headers={"Authorization": "Bearer " + server.token}) as client:
        return await client.post("chat/completions", json=request)


def assert_not_inferred(server, response):
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "RequestPreparationError"
    assert server.adapter.calls == []
    assert CANARY not in response.text + json.dumps(server.records, ensure_ascii=False)
    assert all("request_view" not in record for record in server.records)


@pytest.mark.parametrize("name", ["computer_observe", "computer_act", "computer_finish"])
async def test_exact_history_and_original_tool_content_bytes_survive_reopen(world, name):
    store, task, open_store, _ = world
    session = session_for(store, task)
    messages, results = history(name=name)
    request = payload(messages)
    original = deepcopy(request)
    pin_all(session, results, name)
    prepared = await session.prepare_request(request)
    assert request == original
    assert prepared.payload["messages"][-1] == messages[-1]  # newest observation remains full
    assert prepared.payload["messages"][:2] == messages[:2]
    assert prepared.audit["changed"] is True
    assert prepared.audit["view_chars"] < prepared.audit["source_chars"]
    assert {key: value for key, value in prepared.payload.items() if key != "messages"} == {
        key: value for key, value in original.items() if key != "messages"}
    readback = session.read_view(prepared.audit["id"])
    assert readback["source_messages"] == messages
    assert readback["view_messages"] == prepared.payload["messages"]
    assert restore_messages(readback["view_messages"], readback["manifest"]) == messages
    original_bytes = [message["content"].encode("utf-8") for message in messages if message["role"] == "tool"]
    assert [message["content"].encode("utf-8") for message in readback["source_messages"]
            if message["role"] == "tool"] == original_bytes
    store.close()
    reopened = open_store()
    restored = session_for(reopened, task).read_view(prepared.audit["id"])
    assert restored == readback
    assert reopened._db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert count_rows(reopened, "model_request_views") == 1
    assert CANARY not in json.dumps([event.model_dump(mode="json") for event in safe_events(reopened)], ensure_ascii=False)


async def test_audit_uses_canonical_messages_only_and_returns_independent_copies(world):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    prepared = await session.prepare_request(payload(messages))
    audit = prepared.audit
    source = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    view = json.dumps(prepared.payload["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert audit["source_sha256"] == hashlib.sha256(source.encode()).hexdigest()
    assert audit["view_sha256"] == hashlib.sha256(view.encode()).hexdigest()
    assert audit["source_chars"] == len(source) and audit["view_chars"] == len(view)
    assert audit["changed"] is (audit["source_sha256"] != audit["view_sha256"])
    prepared.payload["messages"][-1]["content"] = "mutated caller copy"
    prepared.audit["source_chars"] = 0
    assert session.read_view(audit["id"])["source_messages"] == messages


@pytest.mark.parametrize("mode", ["none", "other-task", "wrong-tool", "whitespace", "forged-parent-content"])
async def test_unpinned_or_nonidentical_tool_results_are_never_compressed(world, mode):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    if mode == "other-task":
        other = task_at(store, "other-task")
        pin_all(session_for(store, other), results)
    elif mode == "wrong-tool":
        pin_all(session, results, "computer_act")
    elif mode in {"whitespace", "forged-parent-content"}:
        pin_all(session, results)
        for message in messages:
            if message["role"] == "tool":
                parsed = json.loads(message["content"])
                if mode == "forged-parent-content":
                    parsed["text"] += " untrusted mutation"
                message["content"] = json.dumps(parsed, ensure_ascii=False, indent=2)
    prepared = await session.prepare_request(payload(messages))
    assert prepared.payload["messages"] == messages
    assert prepared.audit["changed"] is False


async def test_only_pinned_subset_changes_and_latest_observation_stays_full(world):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history(count=4)
    pin_all(session, [results[0], results[2], results[3]])
    prepared = await session.prepare_request(payload(messages))
    assert prepared.audit["changed"] is True
    assert prepared.payload["messages"][5] == messages[5]  # unpinned result
    assert prepared.payload["messages"][-1] == messages[-1]
    assert session.read_view(prepared.audit["id"])["source_messages"] == messages


def test_pin_is_exact_immutable_deduplicated_and_event_contains_no_content(world):
    store, task, _, _ = world
    session = session_for(store, task)
    result = observation(1)
    expected = json.dumps(result, ensure_ascii=False, allow_nan=False)
    digest = session.register_tool_result("computer_observe", result)
    before = safe_events(store)
    assert session.register_tool_result("computer_observe", deepcopy(result)) == digest
    assert count_rows(store, "model_tool_results") == 1
    assert safe_events(store) == before
    row = store._db.execute("SELECT * FROM model_tool_results").fetchone()
    assert zlib.decompress(row["content"]) == expected.encode("utf-8")
    assert digest == hashlib.sha256(expected.encode()).hexdigest()
    result["text"] = "caller mutation"
    assert zlib.decompress(row["content"]).decode() == expected
    assert CANARY not in json.dumps([event.model_dump(mode="json") for event in before], ensure_ascii=False)


def test_opt_in_required_and_retention_budget_cannot_change_across_reopen(world):
    store, task, open_store, _ = world
    with pytest.raises(TypeError):
        ModelViewSession(store, task.id)
    for mode in (None, "", "production", "diagnostic", False):
        with pytest.raises(AdmissionDenied):
            ModelViewSession(store, task.id, retention_mode=mode)
    session_for(store, task, max_retained_bytes=4096)
    before = safe_events(store)
    session_for(open_store(), task, max_retained_bytes=4096)
    assert safe_events(store) == before
    with pytest.raises(Conflict):
        session_for(open_store(), task, max_retained_bytes=8192)
    assert count_rows(store, "model_view_retention") == 1


@pytest.mark.parametrize("budget", [True, 1024.0, 0, 1023, 134217729, "4096"])
def test_retention_budget_is_bounded_exact_integer(world, budget):
    store, task, _, _ = world
    with pytest.raises(ValueError):
        session_for(store, task, max_retained_bytes=budget)
    assert count_rows(store, "model_view_retention") == 0


async def test_view_ids_and_budget_are_task_scoped(world):
    store, task, _, _ = world
    first = session_for(store, task, max_retained_bytes=4096)
    second_task = task_at(store, "task-second")
    second = session_for(store, second_task, max_retained_bytes=1024)
    request = payload([{"role": "user", "content": "Synthetic task"}])
    one = await first.prepare_request(request)
    two = await second.prepare_request(request)
    assert one.audit["id"] != two.audit["id"]
    with pytest.raises(StoreError):
        second.read_view(one.audit["id"])
    with pytest.raises(StoreError):
        first.read_view(two.audit["id"])
    assert second.read_view(two.audit["id"])["source_messages"] == request["messages"]


async def test_real_http_adapter_reads_committed_view_on_second_connection_before_exactly_one_call(world, endpoint):
    store, task, open_store, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    reader = session_for(open_store(), task)
    inspected = []

    def check_committed(forwarded):
        rows = reader.store._db.execute("SELECT id FROM model_request_views WHERE task_id=?", (task.id,)).fetchall()
        assert len(rows) == 1
        restored = reader.read_view(rows[0]["id"])
        assert restored["view_messages"] == forwarded["messages"]
        assert restored["source_messages"] == messages
        events = safe_events(reader.store)
        assert events[-1].type == "model_view.prepared"
        assert events[-1].redacted_payload == restored["audit"]
        inspected.append(restored["audit"])

    server = await endpoint(session, FakeAdapter(check_committed))
    response = await post(server, payload(messages))
    assert response.status_code == 200
    assert len(server.adapter.calls) == 1 and len(inspected) == 1
    assert server.records[0]["request_view"] == inspected[0]
    assert server.records[0]["status"] == "returned"
    assert CANARY not in json.dumps(server.records, ensure_ascii=False)
    assert server.adapter.calls[0]["messages"][-1] == messages[-1]


async def test_exhausted_budget_rejects_real_http_without_inference_or_partial_row(world, endpoint):
    store, task, _, _ = world
    session = session_for(store, task, max_retained_bytes=1024)
    noise = "".join(random.Random(418).choices(string.ascii_letters + string.digits, k=7000))
    server = await endpoint(session)
    response = await post(server, payload([{"role": "user", "content": CANARY + noise}]))
    assert_not_inferred(server, response)
    assert count_rows(store, "model_request_views") == 0
    assert not any(event.type == "model_view.prepared" for event in safe_events(store))


@pytest.mark.parametrize("messages", [None, {}, [None], ["not-message"]])
async def test_malformed_history_rejects_real_http_without_inference_or_partial_row(world, endpoint, messages):
    store, task, _, _ = world
    server = await endpoint(session_for(store, task))
    response = await post(server, payload(messages))
    assert_not_inferred(server, response)
    assert count_rows(store, "model_request_views") == 0


@pytest.mark.parametrize("damage", ["list-id", "dict-id", "list-function-name", "non-list-calls", "list-result-id"])
async def test_malformed_pin_metadata_cannot_authenticate_or_rewrite_history(world, damage):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    for message in messages:
        if message["role"] == "assistant":
            call = message["tool_calls"][0]
            if damage == "list-id":
                call["id"] = []
            elif damage == "dict-id":
                call["id"] = {"invented": "call-id"}
            elif damage == "list-function-name":
                call["function"]["name"] = ["computer_observe"]
            elif damage == "non-list-calls":
                message["tool_calls"] = {"invented": call}
        elif message["role"] == "tool" and damage == "list-result-id":
            message["tool_call_id"] = []
    prepared = await session.prepare_request(payload(messages))
    assert prepared.payload["messages"] == messages
    assert prepared.audit["changed"] is False
    assert session.read_view(prepared.audit["id"])["source_messages"] == messages


@pytest.mark.parametrize("change", ["pause", "stop"])
@pytest.mark.parametrize("mid_projection", [False, True])
async def test_pause_or_stop_rejects_before_inference_including_revision_race(world, endpoint, monkeypatch, change, mid_projection):
    store, task, open_store, _ = world
    session = session_for(store, task)
    second = open_store()

    def revoke():
        current = second.get_task(task.id)
        method = second.request_pause if change == "pause" else second.request_stop
        method(task.id, current.revision)

    if mid_projection:
        original_project = model_views.project_messages

        def racing_project(*args, **kwargs):
            result = original_project(*args, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(model_views, "project_messages", racing_project)
    else:
        revoke()
    server = await endpoint(session)
    response = await post(server, payload([{"role": "user", "content": CANARY}]))
    assert_not_inferred(server, response)
    assert count_rows(store, "model_request_views") == 0
    assert store.get_task(task.id).status == ("PAUSED" if change == "pause" else "CANCELLING")


async def test_journal_failure_rolls_back_view_and_budget_then_recovery_allows_one_call(world, endpoint, monkeypatch):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    before = safe_events(store)
    original_event = store._event

    def fail_journal(db, current, kind, data, **kwargs):
        if kind == "model_view.prepared":
            raise sqlite3.OperationalError(CANARY + " synthetic disk full")
        return original_event(db, current, kind, data, **kwargs)

    monkeypatch.setattr(store, "_event", fail_journal)
    server = await endpoint(session)
    response = await post(server, payload(messages))
    assert_not_inferred(server, response)
    assert count_rows(store, "model_request_views") == 0
    assert count_rows(store, "model_tool_results") == 3
    assert safe_events(store) == before
    monkeypatch.setattr(store, "_event", original_event)
    response = await post(server, payload(messages))
    assert response.status_code == 200
    assert len(server.adapter.calls) == 1 and count_rows(store, "model_request_views") == 1


@pytest.mark.parametrize("column", ["source", "view", "manifest"])
@pytest.mark.parametrize("damage", ["truncated", "trailing", "invalid-utf8", "oversized-inflate"])
async def test_corrupt_compressed_documents_reject_bounded_readback(world, column, damage):
    store, task, _, _ = world
    session = session_for(store, task)
    prepared = await session.prepare_request(payload([{"role": "user", "content": CANARY}]))
    row = store._db.execute("SELECT * FROM model_request_views").fetchone()
    blob = row[column]
    if damage == "truncated":
        blob = blob[:-2]
    elif damage == "trailing":
        blob += b"unexpected-trailing-member"
    elif damage == "invalid-utf8":
        blob = zlib.compress(b"\xff")
    else:
        limit = model_views.MAX_MANIFEST_BYTES if column == "manifest" else model_views.MAX_JSON_BYTES
        blob = zlib.compress(b"x" * (limit + 1))
    store._db.execute(f"UPDATE model_request_views SET {column}=? WHERE id=?", (blob, prepared.audit["id"]))
    with pytest.raises(StoreError) as error:
        session.read_view(prepared.audit["id"])
    assert CANARY not in str(error.value)


@pytest.mark.parametrize("field,value", [("source_sha256", "0" * 64), ("view_sha256", "0" * 64),
    ("source_chars", 0), ("view_chars", 0), ("id", "different-view"), ("version", "unknown-codec"),
    ("changed", True)])
async def test_tampered_audit_fails_integrity_readback(world, field, value):
    store, task, _, _ = world
    session = session_for(store, task)
    prepared = await session.prepare_request(payload([{"role": "user", "content": CANARY}]))
    audit = prepared.audit | {field: value}
    store._db.execute("UPDATE model_request_views SET audit=? WHERE id=?", (json.dumps(audit), prepared.audit["id"]))
    with pytest.raises(StoreError):
        session.read_view(prepared.audit["id"])


@pytest.mark.parametrize("damage", ["digest-mismatch", "truncated", "oversized-inflate", "unknown-tool"])
async def test_corrupt_retained_parent_pin_rejects_before_real_http_inference(world, endpoint, damage):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    if damage == "unknown-tool":
        store._db.execute("UPDATE model_tool_results SET tool_name='unregistered_host_tool'")
    else:
        blob = zlib.compress(b"{}")
        if damage == "truncated":
            blob = blob[:-2]
        elif damage == "oversized-inflate":
            blob = zlib.compress(b"x" * 96001)
        store._db.execute("UPDATE model_tool_results SET content=?", (blob,))
    server = await endpoint(session)
    response = await post(server, payload(messages))
    assert_not_inferred(server, response)
    assert count_rows(store, "model_request_views") == 0


@pytest.mark.parametrize("value", [float("nan"), float("inf"), b"not-json", {1: "non-string-key"}])
async def test_nonfinite_or_nonjson_source_never_creates_view(world, value):
    store, task, _, _ = world
    session = session_for(store, task)
    with pytest.raises((StoreError, ValueError)):
        await session.prepare_request(payload([{"role": "user", "content": value}]))
    assert count_rows(store, "model_request_views") == 0


def test_duplicate_pin_checks_existing_blob_integrity(world):
    store, task, _, _ = world
    session = session_for(store, task)
    result = observation(1)
    session.register_tool_result("computer_observe", result)
    store._db.execute("UPDATE model_tool_results SET content=?", (zlib.compress(b"{}"),))
    with pytest.raises(StoreError):
        session.register_tool_result("computer_observe", result)


@pytest.mark.parametrize("mutation", ["extra-audit-field", "boolean-length", "integer-changed", "manifest-source-hash", "manifest-original-content"])
async def test_corrupt_audit_types_and_lossless_manifest_are_rejected(world, mutation):
    store, task, _, _ = world
    session = session_for(store, task)
    messages, results = history()
    pin_all(session, results)
    prepared = await session.prepare_request(payload(messages))
    identifier = prepared.audit["id"]
    if mutation.startswith("manifest-"):
        row = store._db.execute("SELECT manifest FROM model_request_views WHERE id=?", (identifier,)).fetchone()
        manifest = json.loads(zlib.decompress(row["manifest"]))
        if mutation == "manifest-source-hash":
            manifest["source_sha256"] = "0" * 64
        else:
            index = next(iter(manifest["original_contents"]))
            manifest["original_contents"][index] += " lost original bytes"
        store._db.execute("UPDATE model_request_views SET manifest=? WHERE id=?",
            (zlib.compress(json.dumps(manifest).encode()), identifier))
    else:
        audit = dict(prepared.audit)
        if mutation == "extra-audit-field":
            audit["raw_prompt"] = CANARY
        elif mutation == "boolean-length":
            audit["source_chars"] = True
        else:
            audit["changed"] = 1
        store._db.execute("UPDATE model_request_views SET audit=? WHERE id=?", (json.dumps(audit), identifier))
    with pytest.raises(StoreError) as error:
        session.read_view(identifier)
    assert CANARY not in str(error.value)


async def test_document_limit_counts_utf8_before_compression(world):
    store, task, _, _ = world
    session = session_for(store, task)
    # Highly compressible; storage budget alone would accept it. The retained
    # raw UTF-8 document boundary must still reject it before creating a view.
    request = payload([{"role": "user", "content": "中" * (model_views.MAX_JSON_BYTES // 3 + 1)}])
    with pytest.raises(AdmissionDenied):
        await session.prepare_request(request)
    assert count_rows(store, "model_request_views") == 0
    assert len(canonical_messages(request["messages"]).encode()) > model_views.MAX_JSON_BYTES


@pytest.mark.parametrize("name,result", [("unregistered_host_tool", {}), ("computer_observe", []),
    ("computer_observe", {"text": "x" * 24000})])
def test_unapproved_or_oversized_pin_never_gets_retained(world, name, result):
    store, task, _, _ = world
    session = session_for(store, task)
    before = safe_events(store)
    with pytest.raises(AdmissionDenied):
        session.register_tool_result(name, result)
    assert count_rows(store, "model_tool_results") == 0
    assert safe_events(store) == before


async def test_v3_to_v4_additive_migration_preserves_task_events_and_policy_then_accepts_view(world):
    store, task, open_store, path = world
    store.pin_policy(task.policy_ref, "a" * 64)
    original_task = store.get_task(task.id)
    original_events = store.events(task.id)
    original_policy = tuple(store._db.execute("SELECT * FROM policies WHERE id=?", (task.policy_ref,)).fetchone())
    store.close()
    # The exact pre-v4 additive boundary: leave all authority/history tables
    # untouched and remove only the three model-retention tables.
    with sqlite3.connect(path) as legacy:
        for table in ("model_request_views", "model_tool_results", "model_view_retention"):
            legacy.execute(f"DROP TABLE {table}")
        legacy.execute("PRAGMA user_version=3")
    reopened = open_store()
    assert reopened._db.execute("PRAGMA user_version").fetchone()[0] == 4
    assert reopened.get_task(task.id) == original_task
    assert reopened.events(task.id) == original_events
    assert tuple(reopened._db.execute("SELECT * FROM policies WHERE id=?", (task.policy_ref,)).fetchone()) == original_policy
    reopened.assert_policy(task.policy_ref, "a" * 64)
    with pytest.raises(AdmissionDenied):
        reopened.assert_policy(task.policy_ref, "b" * 64)
    session = session_for(reopened, task)
    messages, results = history()
    pin_all(session, results)
    prepared = await session.prepare_request(payload(messages))
    assert session.read_view(prepared.audit["id"])["source_messages"] == messages
    assert count_rows(reopened, "model_tool_results") == 3
    assert count_rows(reopened, "model_request_views") == 1
