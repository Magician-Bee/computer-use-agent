"""Real ephemeral loopback HTTP; every inference adapter is an in-memory fake."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from urllib.parse import urlsplit

import httpx
import pytest
import pytest_asyncio

from server.model_server import LocalModelServer, PreparedModelRequest


RESULT = {"id": "chatcmpl-fixture", "object": "chat.completion", "created": 42,
    "model": "fixture-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
        "tool_calls": [{"id": "call-fixture", "type": "function", "function": {
            "name": "computer_observe", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    "ollama": {"prompt_eval_count": 10, "eval_count": 5}}
PAYLOAD = {"model": "fixture-model", "messages": [{"role": "user", "content": "Read the test page"}]}


class FakeAdapter:
    def __init__(self, *, blocked=False, error=None, hold_cancel=False):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.blocked, self.error, self.hold_cancel = blocked, error, hold_cancel

    async def complete(self, payload):
        self.calls.append(deepcopy(payload))
        self.started.set()
        try:
            if self.blocked:
                await self.release.wait()
            if self.error:
                raise self.error
            return deepcopy(RESULT)
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.hold_cancel:
                await self.cleanup_release.wait()
            raise


@pytest_asyncio.fixture
async def endpoint():
    instances = []

    async def create(adapter=None, *, prepare_request=None):
        adapter = adapter or FakeAdapter()
        server = LocalModelServer(adapter, model="fixture-model", context_length=64000, prepare_request=prepare_request)
        instances.append(server)
        await server.__aenter__()
        return server

    yield create
    for server in reversed(instances):
        server.adapter.cleanup_release.set()
        if hasattr(server.prepare_request, "cleanup_release"):
            server.prepare_request.cleanup_release.set()
        await asyncio.wait_for(server.close(), 6)


def client(server):
    return httpx.AsyncClient(base_url=server.url, headers={"Authorization": "Bearer " + server.token},
                            trust_env=False, timeout=4)


async def eventually(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def assert_socket_closed(server):
    assert server._socket.fileno() == -1
    assert server._serve_task.done()
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", urlsplit(server.url).port)


async def test_context_overflow_preserves_safe_machine_code_without_request_content(endpoint):
    class ContextOverflow(Exception):
        code = "context_length_exceeded"
    server = await endpoint(FakeAdapter(error=ContextOverflow("CANARY-secret-context")))
    async with client(server) as http:
        response = await http.post("/chat/completions", json=PAYLOAD)
    assert response.status_code == 400
    assert response.json()["error"] == {"message": "Local model context capacity exceeded",
        "type": "ContextOverflow", "code": "context_length_exceeded"}
    assert "CANARY" not in response.text + json.dumps(server.records)
    assert server.records[0]["error_code"] == "context_length_exceeded"


@pytest.mark.parametrize("mode", ["missing", "wrong", "basic", "non-ascii", "origin", "fetch-same-origin", "fetch-none", "fetch-cross-site"])
async def test_boundary_rejects_missing_wrong_or_browser_credentials_without_adapter_calls(endpoint, mode):
    server = await endpoint()
    headers = [(b"authorization", ("Bearer " + server.token).encode())]
    if mode == "missing": headers = []
    elif mode == "wrong": headers = [(b"authorization", b"Bearer CANARY-wrong-token")]
    elif mode == "basic": headers = [(b"authorization", ("Basic " + server.token).encode())]
    elif mode == "non-ascii": headers = [(b"authorization", b"Bearer \xe9")]
    elif mode == "origin": headers.append((b"origin", b"http://127.0.0.1"))
    else: headers.append((b"sec-fetch-site", mode.removeprefix("fetch-").encode()))
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.get(server.url + "/models", headers=headers)
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    assert server.token not in response.text and "CANARY" not in response.text
    assert server.adapter.calls == []


async def test_models_is_authenticated_task_scoped_and_not_an_inference(endpoint):
    server = await endpoint()
    async with client(server) as http:
        response = await http.get("models")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [{"id": "fixture-model", "object": "model", "context_length": 64000}]}
    assert response.headers["cache-control"] == "no-store"
    assert server.adapter.calls == [] and server.records == []
    assert urlsplit(server.url).hostname == "127.0.0.1" and urlsplit(server.url).port != 8765


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
async def test_completion_requires_json_media_type(endpoint, content_type):
    server = await endpoint()
    headers = {} if content_type is None else {"Content-Type": content_type}
    async with client(server) as http:
        response = await http.post("chat/completions", content=json.dumps(PAYLOAD), headers=headers)
    assert response.status_code == 415
    assert response.headers["cache-control"] == "no-store"
    assert server.adapter.calls == []


@pytest.mark.parametrize("body", [b"", b"null", b"[]", b"{}", b"{bad", b"\xff",
    json.dumps(PAYLOAD | {"model": "different-model"}).encode(),
    json.dumps(PAYLOAD | {"stream": "true"}).encode(),
    json.dumps(PAYLOAD | {"stream": 1}).encode(),
    b'{"model":"fixture-model","temperature":NaN}',
    b'{"model":"fixture-model","temperature":1e9999}',
    b'{"model":"fixture-model","messages":'+b'['*2000+b'0'+b']'*2000+b'}'],
    ids=["empty", "null", "array", "empty-object", "syntax", "encoding", "wrong-model",
         "string-stream", "integer-stream", "nan", "overflow-float", "deep-json"])
async def test_invalid_json_model_and_stream_are_rejected_before_adapter(endpoint, body):
    server = await endpoint()
    async with client(server) as http:
        response = await http.post("chat/completions", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert server.adapter.calls == [] and server.records == []


@pytest.mark.parametrize("chunked", [False, True])
async def test_body_limit_rejects_content_length_and_incremental_chunked_upload(endpoint, chunked):
    server = await endpoint()
    oversized = b"x" * (2_097_152 + 1)
    async def chunks():
        for offset in range(0, len(oversized), 65536):
            yield oversized[offset:offset + 65536]
    async with client(server) as http:
        response = await http.post("chat/completions", content=chunks() if chunked else oversized,
                                   headers={"Content-Type": "application/json"})
    assert response.status_code == 413
    assert server.adapter.calls == []


async def test_nonstream_forwards_exactly_once_and_keeps_only_safe_record_metadata(endpoint):
    server = await endpoint()
    payload = PAYLOAD | {"stream": False, "max_tokens": 20}
    async with client(server) as http:
        response = await http.post("chat/completions", json=payload)
    assert response.status_code == 200 and response.json() == RESULT
    assert server.adapter.calls == [payload]
    assert server.records[0]["status"] == "returned"
    assert server.records[0]["usage"] == RESULT["usage"]
    assert "messages" not in server.records[0] and "content" not in server.records[0]
    assert "elapsed_seconds" in server.records[0]


async def test_stream_is_buffered_protocol_conversion_with_tool_indices_usage_and_done(endpoint):
    server = await endpoint()
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD | {"stream": True})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert frames.pop() == "[DONE]"
    first, final, usage = [json.loads(frame) for frame in frames]
    assert first["choices"][0]["delta"]["tool_calls"][0] == RESULT["choices"][0]["message"]["tool_calls"][0] | {"index": 0}
    assert first["choices"][0]["finish_reason"] is None
    assert final["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "tool_calls"}
    assert usage["choices"] == [] and usage["usage"] == RESULT["usage"]
    assert all(item["object"] == "chat.completion.chunk" for item in (first, final, usage))
    assert len(server.adapter.calls) == 1
    assert "index" not in RESULT["choices"][0]["message"]["tool_calls"][0]


async def test_concurrent_completion_is_rejected_and_next_request_can_run(endpoint):
    adapter = FakeAdapter(blocked=True)
    server = await endpoint(adapter)
    async with client(server) as http:
        first = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(adapter.started.wait(), 2)
        second = await http.post("chat/completions", json=PAYLOAD)
        assert second.status_code == 409 and len(adapter.calls) == 1
        adapter.release.set()
        assert (await first).status_code == 200
        assert (await http.post("chat/completions", json=PAYLOAD)).status_code == 200
    assert len(adapter.calls) == 2 and not server._active and not server._busy


async def test_adapter_failure_does_not_return_exception_message_or_prompt(endpoint):
    server = await endpoint(FakeAdapter(error=ValueError("CANARY-secret-adapter-error")))
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == 400
    assert response.json() == {"error": {"message": "Local model adapter failed", "type": "ValueError"}}
    assert "CANARY" not in response.text + json.dumps(server.records)
    assert server.records[0]["status"] == "failed"
    assert not server._active and not server._busy


async def test_synchronous_adapter_setup_failure_is_safe_and_does_not_leave_busy_latch(endpoint):
    server = await endpoint()
    def fail_before_coroutine(_payload):
        raise ValueError("CANARY-synchronous-adapter-error")
    server.adapter.complete = fail_before_coroutine
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
        retry = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == retry.status_code == 400
    assert "CANARY" not in response.text + json.dumps(server.records)
    assert not server._active and not server._busy


async def test_real_client_disconnect_cancels_and_drains_adapter(endpoint):
    adapter = FakeAdapter(blocked=True)
    server = await endpoint(adapter)
    body = json.dumps(PAYLOAD).encode()
    _, writer = await asyncio.open_connection("127.0.0.1", urlsplit(server.url).port)
    writer.write((f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {server.token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
    await writer.drain()
    await asyncio.wait_for(adapter.started.wait(), 2)
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(adapter.cancelled.wait(), 2)
    await eventually(lambda: not server._active and not server._busy)
    assert server.records[0]["status"] == "client_disconnected"


async def test_close_cancels_active_inference_and_releases_owned_http_socket(endpoint):
    adapter = FakeAdapter(blocked=True)
    server = await endpoint(adapter)
    async with client(server) as http:
        request = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(adapter.started.wait(), 2)
        await asyncio.wait_for(server.close(), 5)
        await asyncio.gather(request, return_exceptions=True)
    assert adapter.cancelled.is_set() and not server._active
    assert server.records[0]["status"] == "cancelled"
    await assert_socket_closed(server)


async def test_cancelled_close_caller_cannot_cancel_owned_cleanup_or_leak_listener(endpoint):
    adapter = FakeAdapter(blocked=True, hold_cancel=True)
    server = await endpoint(adapter)
    async with client(server) as http:
        request = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(adapter.started.wait(), 2)
        closing = asyncio.create_task(server.close())
        await asyncio.wait_for(adapter.cancelled.wait(), 2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        try:
            assert server._close_task is not None and not server._close_task.done()
        finally:
            adapter.cleanup_release.set()
        await asyncio.wait_for(server.close(), 5)
        await asyncio.gather(request, return_exceptions=True)
    await assert_socket_closed(server)


async def test_close_is_idempotent_and_server_cannot_be_reentered(endpoint):
    server = await endpoint()
    with pytest.raises(RuntimeError, match="one task lifetime"):
        await server.__aenter__()
    await asyncio.gather(server.close(), server.close(), server.close())
    first_cleanup = server._close_task
    await server.close()
    assert server._close_task is first_cleanup
    with pytest.raises(RuntimeError, match="one task lifetime"):
        await server.__aenter__()
    await assert_socket_closed(server)


async def test_failed_http_startup_still_closes_socket_and_close_remains_idempotent(monkeypatch):
    import server.model_server as module

    async def failed_startup(_self, **_kwargs):
        raise RuntimeError("fixture startup failure")
    monkeypatch.setattr(module.uvicorn.Server, "serve", failed_startup)
    server = LocalModelServer(FakeAdapter(), model="fixture-model", context_length=64000)
    with pytest.raises(RuntimeError, match="fixture startup failure"):
        await server.__aenter__()
    await server.close()
    await assert_socket_closed(server)


def prepared_view(payload):
    source = json.dumps(payload["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    view = deepcopy(payload)
    view["messages"] = [{"role": "user", "content": "父程序建立的合成 context view"}]
    projected = json.dumps(view["messages"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return PreparedModelRequest(payload=view, audit={"id": "view-fixture-1", "version": "context.v1",
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "view_sha256": hashlib.sha256(projected.encode()).hexdigest(),
        "source_chars": len(source), "view_chars": len(projected), "changed": source != projected})


class FakePreparer:
    def __init__(self, *, blocked=False, hold_cancel=False, swallow_cancel=False):
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.blocked, self.hold_cancel, self.swallow_cancel = blocked, hold_cancel, swallow_cancel
        self.result = None

    async def __call__(self, payload):
        self.calls.append(deepcopy(payload))
        self.started.set()
        try:
            if self.blocked:
                await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.hold_cancel:
                await self.cleanup_release.wait()
            if not self.swallow_cancel:
                raise
        self.result = prepared_view(payload)
        return self.result


@pytest.mark.parametrize("stream", [False, True])
async def test_parent_preparation_runs_once_before_exactly_one_adapter_call(endpoint, stream):
    prepare = FakePreparer()
    server = await endpoint(prepare_request=prepare)
    payload = PAYLOAD | {"stream": stream, "max_tokens": 31,
        "tools": [{"type": "function", "function": {"name": "computer_observe"}}]}
    async with client(server) as http:
        response = await http.post("chat/completions", json=payload)
    assert response.status_code == 200
    assert prepare.calls == [payload]
    assert server.adapter.calls == [prepared_view(payload).payload]
    assert server.records[0]["request_view"] == prepared_view(payload).audit
    assert server.records[0]["status"] == "returned"
    assert "父程序" not in json.dumps(server.records, ensure_ascii=False)
    assert "Read the test page" not in json.dumps(server.records)
    assert response.headers["content-type"].startswith("text/event-stream" if stream else "application/json")


async def test_preparer_gets_private_input_and_admitted_view_and_audit_are_detached(endpoint):
    class RetainingAdapter(FakeAdapter):
        async def complete(self, payload):
            self.received_reference = payload
            return await super().complete(payload)

    returned = []
    async def prepare(payload):
        # Mutating this input must not mutate the server's original protocol
        # fields, which would otherwise defeat the messages-only comparison.
        result = prepared_view(payload)
        payload["stream"] = True
        payload["messages"][0]["content"] = "CANARY-mutated-source"
        returned.append(result)
        return result

    adapter = RetainingAdapter(blocked=True)
    server = await endpoint(adapter, prepare_request=prepare)
    async with client(server) as http:
        request = asyncio.create_task(http.post("chat/completions", json=PAYLOAD | {"stream": False}))
        await asyncio.wait_for(adapter.started.wait(), 2)
        returned[0].payload["messages"][0]["content"] = "CANARY-late-mutation"
        returned[0].audit["id"] = "CANARY-late-audit"
        assert adapter.received_reference["messages"][0]["content"] == "父程序建立的合成 context view"
        assert server.records[0]["request_view"]["id"] == "view-fixture-1"
        adapter.release.set()
        response = await request
    assert response.status_code == 200 and response.headers["content-type"].startswith("application/json")
    assert "CANARY" not in json.dumps(server.records)


@pytest.mark.parametrize("changed", [
    {"model": "CANARY-another-model"}, {"stream": True}, {"max_tokens": 21},
    {"temperature": True}, {"temperature": 1.0}, {"extra_provider_setting": "CANARY-secret"},
    {"tools": []}, {"tool_choice": "required"},
])
async def test_preparation_cannot_change_any_non_message_protocol_field(endpoint, changed):
    payload = PAYLOAD | {"stream": False, "max_tokens": 20, "temperature": 1,
        "tools": [{"type": "function", "function": {"name": "computer_observe"}}], "tool_choice": "auto"}
    async def prepare(source):
        result = prepared_view(source)
        result.payload.update(changed)
        return result

    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        response = await http.post("chat/completions", json=payload)
    assert response.status_code == 400
    assert response.json()["error"] == {"message": "Local model request preparation failed", "type": "RequestPreparationError"}
    assert server.adapter.calls == [] and "request_view" not in server.records[0]
    assert "CANARY" not in response.text + json.dumps(server.records)


async def test_preparation_cannot_delete_other_fields_or_mutate_its_input_to_evade_comparison(endpoint):
    async def prepare(source):
        source.pop("max_tokens")
        source["tools"][0]["function"]["name"] = "CANARY-forbidden-tool"
        return prepared_view(source)

    server = await endpoint(prepare_request=prepare)
    payload = PAYLOAD | {"max_tokens": 20, "tools": [{"type": "function", "function": {"name": "computer_observe"}}]}
    async with client(server) as http:
        response = await http.post("chat/completions", json=payload)
    assert response.status_code == 400 and server.adapter.calls == []
    assert "CANARY" not in response.text + json.dumps(server.records)


@pytest.mark.parametrize("kind", ["sync-return", "sync-error", "async-error", "none", "dict", "wrong-payload", "wrong-audit"])
async def test_bad_preparer_or_bad_result_fails_closed_without_leaking_errors(endpoint, kind):
    async def prepare(source):
        if kind == "async-error": raise ValueError("CANARY-raw-parent-error")
        if kind == "none": return None
        if kind == "dict": return {"payload": source, "audit": {"raw": "CANARY"}}
        if kind == "wrong-payload": return PreparedModelRequest([], prepared_view(source).audit)
        if kind == "wrong-audit": return PreparedModelRequest(source, [])
        raise AssertionError("unexpected fixture case")
    if kind == "sync-return":
        prepare = prepared_view
    elif kind == "sync-error":
        def prepare(_source): raise RuntimeError("CANARY-sync-parent-error")

    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        first = await http.post("chat/completions", json=PAYLOAD)
        second = await http.post("chat/completions", json=PAYLOAD)
    assert first.status_code == second.status_code == 400
    assert first.json()["error"]["type"] == "RequestPreparationError"
    assert "CANARY" not in first.text + json.dumps(server.records)
    assert server.adapter.calls == [] and not server._busy and not server._active


@pytest.mark.parametrize("messages", [None, [], "CANARY", [None], [{"content": float("nan")}],
    [{"content": float("inf")}], [{"content": b"CANARY"}], [{1: "CANARY"}], [{"content": "\ud800"}],
    [{"content": "x" * 2_097_153}], [{"content": "字" * 800000}]])
async def test_prepared_messages_must_be_finite_bounded_plain_json(endpoint, messages):
    async def prepare(source):
        result = prepared_view(source)
        result.payload["messages"] = messages
        return result
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == 400 and server.adapter.calls == []
    assert "CANARY" not in response.text + json.dumps(server.records)


async def test_recursive_prepared_json_fails_closed(endpoint):
    async def prepare(source):
        result = prepared_view(source)
        result.payload["messages"][0]["content"] = result.payload
        return result
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == 400 and server.adapter.calls == []


@pytest.mark.parametrize("change", [
    {"raw_prompt": "CANARY-private-history"}, {"id": "CANARY/private/path"}, {"id": "x" * 129},
    {"version": "private\nCANARY"}, {"version": 1}, {"source_sha256": "CANARY"},
    {"view_sha256": "A" * 64}, {"source_chars": True}, {"view_chars": -1},
    {"view_chars": 2_097_153}, {"changed": 1},
])
async def test_request_view_audit_has_only_bounded_safe_metadata(endpoint, change):
    async def prepare(source):
        result = prepared_view(source)
        result.audit.update(change)
        return result
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == 400 and server.adapter.calls == []
    assert "request_view" not in server.records[0]
    assert "CANARY" not in response.text + json.dumps(server.records)


@pytest.mark.parametrize("field", ["source_sha256", "view_sha256", "source_chars", "view_chars", "changed", "missing-id"])
async def test_prepared_audit_must_match_actual_canonical_message_content(endpoint, field):
    async def prepare(source):
        result = prepared_view(source)
        if field.endswith("sha256"):
            result.audit[field] = "0" * 64  # Well-formed but not the actual messages.
        elif field.endswith("chars"):
            result.audit[field] += 1
        elif field == "changed":
            result.audit[field] = False
        else:
            del result.audit["id"]
        return result

    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        response = await http.post("chat/completions", json=PAYLOAD)
    assert response.status_code == 400 and server.adapter.calls == []
    assert "request_view" not in server.records[0]


async def test_unchanged_message_projection_has_equal_hashes_and_ignores_dict_key_order(endpoint):
    async def prepare(source):
        result = prepared_view(source)
        result.payload["messages"] = [{"content": message["content"], "role": message["role"]}
                                      for message in source["messages"]]
        canonical = json.dumps(source["messages"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        result.audit.update(source_sha256=digest, view_sha256=digest,
                            source_chars=len(canonical), view_chars=len(canonical), changed=False)
        return result

    server = await endpoint(prepare_request=prepare)
    payload = PAYLOAD | {"messages": [{"role": "user", "content": "原始繁體任務\n\"quoted\""}], "stream": False}
    async with client(server) as http:
        response = await http.post("chat/completions", json=payload)
    assert response.status_code == 200 and server.adapter.calls == [payload]
    audit = server.records[0]["request_view"]
    assert audit["source_sha256"] == audit["view_sha256"] and audit["changed"] is False


async def test_preparation_occupies_same_busy_slot_as_inference(endpoint):
    prepare = FakePreparer(blocked=True)
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        first = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(prepare.started.wait(), 2)
        second = await http.post("chat/completions", json=PAYLOAD)
        assert second.status_code == 409
        assert len(prepare.calls) == 1 and server.adapter.calls == []
        prepare.release.set()
        assert (await first).status_code == 200
    assert len(server.adapter.calls) == 1


@pytest.mark.parametrize("swallow_cancel", [False, True])
async def test_real_disconnect_during_preparation_never_starts_adapter(endpoint, swallow_cancel):
    prepare = FakePreparer(blocked=True, swallow_cancel=swallow_cancel)
    server = await endpoint(prepare_request=prepare)
    body = json.dumps(PAYLOAD).encode()
    _, writer = await asyncio.open_connection("127.0.0.1", urlsplit(server.url).port)
    writer.write((f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {server.token}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode() + body)
    await writer.drain()
    await asyncio.wait_for(prepare.started.wait(), 2)
    writer.close()
    await writer.wait_closed()
    await asyncio.wait_for(prepare.cancelled.wait(), 2)
    await eventually(lambda: not server._active and not server._busy)
    assert server.adapter.calls == []
    assert server.records[0]["status"] == "client_disconnected"
    assert "request_view" not in server.records[0]


@pytest.mark.parametrize("swallow_cancel", [False, True])
async def test_close_during_preparation_drains_it_and_never_starts_adapter(endpoint, swallow_cancel):
    prepare = FakePreparer(blocked=True, swallow_cancel=swallow_cancel)
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        request = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(prepare.started.wait(), 2)
        await asyncio.wait_for(server.close(), 5)
        await asyncio.gather(request, return_exceptions=True)
    assert prepare.cancelled.is_set()
    assert server.adapter.calls == [] and not server._active
    assert server.records[0]["status"] == "cancelled"
    await assert_socket_closed(server)


async def test_cancelled_close_waiter_cannot_abandon_preparation_cleanup(endpoint):
    prepare = FakePreparer(blocked=True, hold_cancel=True)
    server = await endpoint(prepare_request=prepare)
    async with client(server) as http:
        request = asyncio.create_task(http.post("chat/completions", json=PAYLOAD))
        await asyncio.wait_for(prepare.started.wait(), 2)
        closing = asyncio.create_task(server.close())
        await asyncio.wait_for(prepare.cancelled.wait(), 2)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not server._close_task.done() and server.adapter.calls == []
        prepare.cleanup_release.set()
        await asyncio.wait_for(server.close(), 5)
        await asyncio.gather(request, return_exceptions=True)
    assert server.adapter.calls == []
    await assert_socket_closed(server)
