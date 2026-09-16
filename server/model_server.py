"""Task-owned, authenticated loopback endpoint for the Hermes model adapter.

No separate conversation loop lives here. Each HTTP completion is forwarded
once to an adapter and serialized back into the protocol Hermes requested.
"""
from __future__ import annotations

import asyncio
import contextlib
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import secrets
import socket
import time
import uuid
from typing import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
import uvicorn


@dataclass(frozen=True)
class PreparedModelRequest:
    """Parent context view; only messages may differ, with canonical-message audit."""

    payload: dict
    audit: dict


class RequestPreparationError(ValueError):
    """Safe boundary error: never includes callback messages or payload values."""


_REQUEST_LIMIT = 2_097_152
_AUDIT_FIELDS = frozenset({"id", "version", "source_sha256", "view_sha256",
                            "source_chars", "view_chars", "changed"})


def _bounded_json(value, *, limit=_REQUEST_LIMIT):
    """Canonical plain finite JSON, rejecting object coercion and excess depth."""
    nodes = 0

    def check(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 64 or nodes > 100000:
            raise RequestPreparationError
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise RequestPreparationError
                check(key, depth + 1)
                check(child, depth + 1)
        elif type(item) is list:
            for child in item:
                check(child, depth + 1)
        elif type(item) is str:
            if len(item) > limit:
                raise RequestPreparationError
        elif type(item) is float:
            if not math.isfinite(item):
                raise RequestPreparationError
        elif type(item) not in (int, bool, type(None)):
            raise RequestPreparationError

    check(value, 0)
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    parts, size = [], 0
    for part in encoder.iterencode(value):
        size += len(part.encode("utf-8"))
        if size > limit:
            raise RequestPreparationError
        parts.append(part)
    return "".join(parts)


def _prepared_request(source: dict, result: PreparedModelRequest):
    if type(result) is not PreparedModelRequest or type(result.payload) is not dict or type(result.audit) is not dict:
        raise RequestPreparationError
    # Serialize before any await: later mutation by callback-owned objects must
    # not change the admitted view, protocol fields, or metadata in records.
    raw = _bounded_json(result.payload)
    payload = json.loads(raw)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or not all(type(message) is dict for message in messages):
        raise RequestPreparationError
    unchanged_source = {key: value for key, value in source.items() if key != "messages"}
    unchanged_view = {key: value for key, value in payload.items() if key != "messages"}
    # JSON identity distinguishes booleans, integers, floats and absent fields;
    # Python dict equality alone would incorrectly accept True == 1.
    if _bounded_json(unchanged_source) != _bounded_json(unchanged_view):
        raise RequestPreparationError
    audit = result.audit
    if set(audit) != _AUDIT_FIELDS:
        raise RequestPreparationError
    for field, maximum in (("id", 128), ("version", 64)):
        if type(audit[field]) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,"+str(maximum-1)+r"}", audit[field]):
            raise RequestPreparationError
    for field in ("source_sha256", "view_sha256"):
        if type(audit[field]) is not str or not re.fullmatch(r"[0-9a-f]{64}", audit[field]):
            raise RequestPreparationError
    for field in ("source_chars", "view_chars"):
        if type(audit[field]) is not int or not 0 <= audit[field] <= _REQUEST_LIMIT:
            raise RequestPreparationError
    if type(audit["changed"]) is not bool:
        raise RequestPreparationError
    # Independently bind the parent's persisted-view metadata to exactly the
    # messages crossing this boundary, with the same canonical codec. Other
    # request fields have already been checked separately and are not hashed.
    source_messages = _bounded_json(source["messages"])
    view_messages = _bounded_json(messages)
    source_sha = hashlib.sha256(source_messages.encode("utf-8")).hexdigest()
    view_sha = hashlib.sha256(view_messages.encode("utf-8")).hexdigest()
    if (not hmac.compare_digest(audit["source_sha256"], source_sha)
            or not hmac.compare_digest(audit["view_sha256"], view_sha)
            or audit["source_chars"] != len(source_messages) or audit["view_chars"] != len(view_messages)
            or audit["changed"] is not (source_sha != view_sha)):
        raise RequestPreparationError
    return payload, json.loads(_bounded_json(audit, limit=2048))


class _BoundaryMiddleware:
    """Leave ASGI receive untouched so disconnect/cancellation stay observable."""

    def __init__(self, app, check):
        self.app, self.check = app, check

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def no_store(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [(name, value) for name, value in message.get("headers", [])
                                      if name.lower() != b"cache-control"] + [(b"cache-control", b"no-store")]
            await send(message)

        rejected = self.check(Request(scope, receive))
        if rejected is not None:
            return await rejected(scope, receive, no_store)
        await self.app(scope, receive, no_store)


def _invalid_json_constant(_value):
    raise ValueError("Non-finite JSON number")


def _finite_json_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Non-finite JSON number")
    return parsed


class LocalModelServer:
    def __init__(self, adapter, *, model: str, context_length: int,
                 prepare_request: Callable[[dict], Awaitable[PreparedModelRequest]] | None = None):
        self.adapter, self.model, self.context_length = adapter, model, context_length
        self.prepare_request = prepare_request
        self.token = secrets.token_urlsafe(32)
        self.url: str | None = None
        self._socket = self._server = self._serve_task = None
        self._close_task: asyncio.Task | None = None
        self._active: set[asyncio.Task] = set()
        self._closing = False
        self._busy = False
        self.records: list[dict] = []
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        self.app.add_middleware(_BoundaryMiddleware, check=self._boundary)
        self.app.add_api_route("/v1/models", self._models, methods=["GET"])
        self.app.add_api_route("/v1/chat/completions", self._completion, methods=["POST"])

    def _boundary(self, request: Request):
        if (request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None
                or not hmac.compare_digest(request.headers.get("authorization", "").encode("utf-8"),
                                           ("Bearer "+self.token).encode("ascii"))):
            return JSONResponse({"error": {"message": "Unauthorized local model request"}}, status_code=403)
        if self._closing:
            return JSONResponse({"error": {"message": "Model task is closing"}}, status_code=503)
        if request.method == "POST" and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return JSONResponse({"error": {"message": "JSON body required"}}, status_code=415)
        return None

    async def _models(self):
        return {"object": "list", "data": [{"id": self.model, "object": "model", "context_length": self.context_length}]}

    async def _completion(self, request: Request):
        body = bytearray()
        async for part in request.stream():
            if len(body) + len(part) > _REQUEST_LIMIT:
                return JSONResponse({"error": {"message": "Model request exceeds task limit"}}, status_code=413)
            body.extend(part)
        try:
            payload = json.loads(body, parse_constant=_invalid_json_constant, parse_float=_finite_json_float)
            if not isinstance(payload, dict) or payload.get("model") != self.model or type(payload.get("stream", False)) is not bool:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            return JSONResponse({"error": {"message": "Invalid task model request"}}, status_code=400)
        if self._busy or self._closing:
            return JSONResponse({"error": {"message": "Task already has an active model call"}}, status_code=409)
        self._busy = True
        began = time.monotonic()
        record = {"model": self.model, "context_length": self.context_length, "status": "running"}
        self.records.append(record)

        async def forward_once():
            forwarded = payload
            if self.prepare_request is not None:
                try:
                    prepared = await self.prepare_request(deepcopy(payload))
                    # A callback that swallowed cancellation must not cause a
                    # late inference after stop/disconnect revoked this call.
                    if self._closing or asyncio.current_task().cancelling():
                        raise asyncio.CancelledError
                    forwarded, audit = _prepared_request(payload, prepared)
                    record["request_view"] = audit
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RequestPreparationError from None
                await asyncio.sleep(0)
                if self._closing or asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
            # Even a synchronous adapter setup error belongs to the same
            # bounded error/cleanup path as an asynchronously failed request.
            return await self.adapter.complete(forwarded)

        call = asyncio.create_task(forward_once())
        self._active.add(call)

        async def disconnected():
            # The body has already been fully consumed. A blocking receive is
            # cancellable and does not use is_disconnected()'s polling cancel
            # scope, which can swallow watcher cancellation in middleware.
            while True:
                if (await request.receive())["type"] == "http.disconnect":
                    return
        watcher = asyncio.create_task(disconnected())
        try:
            finished, _ = await asyncio.wait((call, watcher), return_when=asyncio.FIRST_COMPLETED)
            if watcher in finished and call not in finished:
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                record["status"] = "client_disconnected"
                return Response(status_code=499)
            result = await call
            record.update(status="returned", usage=result.get("usage"), ollama=result.get("ollama"))
            if not payload.get("stream"):
                return JSONResponse(result)
            message = result["choices"][0]["message"]
            delta = dict(message)
            if message.get("tool_calls"):
                delta["tool_calls"] = [{**item, "index": i} for i, item in enumerate(message["tool_calls"])]
            common = {"id": result.get("id", "chatcmpl-"+uuid.uuid4().hex), "object": "chat.completion.chunk",
                      "created": result.get("created", int(time.time())), "model": self.model}
            chunks = [{**common, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                      {**common, "choices": [{"index": 0, "delta": {}, "finish_reason": result["choices"][0]["finish_reason"]}]},
                      {**common, "choices": [], "usage": result.get("usage", {})}]
            raw = "".join("data: "+json.dumps(item, ensure_ascii=False)+"\n\n" for item in chunks)+"data: [DONE]\n\n"
            # Inference is complete; this SSE body is protocol conversion, not
            # a claim that individual model tokens were streamed live.
            return Response(raw, media_type="text/event-stream")
        except asyncio.CancelledError:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            record["status"] = "cancelled"
            if self._closing:
                return JSONResponse({"error": {"message": "Model task is closing"}}, status_code=503)
            raise
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            error = {"message": "Local model adapter failed", "type": type(exc).__name__}
            if isinstance(exc, RequestPreparationError):
                error["message"] = "Local model request preparation failed"
            if getattr(exc, "code", None) == "context_length_exceeded":
                error.update(code="context_length_exceeded", message="Local model context capacity exceeded")
                record["error_code"] = "context_length_exceeded"
            return JSONResponse({"error": error}, status_code=400)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            self._active.discard(call)
            record["elapsed_seconds"] = round(time.monotonic()-began, 3)
            self._busy = False

    async def __aenter__(self):
        if self._socket is not None or self._closing:
            raise RuntimeError("A model endpoint belongs to one task lifetime")
        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.bind(("127.0.0.1", 0))
            self._socket.listen(32)
            self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}/v1"
            self._server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=0, lifespan="off",
                                                       access_log=False, log_level="critical", timeout_graceful_shutdown=2))
            self._serve_task = asyncio.create_task(self._server.serve(sockets=[self._socket]))
            async with asyncio.timeout(5):
                while not self._server.started:
                    if self._serve_task.done():
                        await self._serve_task
                        raise RuntimeError("Model endpoint exited before startup")
                    await asyncio.sleep(0.01)
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *_args):
        await self.close()

    async def close(self):
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close_owned())
        # Each caller owns only its wait, never the task lifetime's cleanup.
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        try:
            if self._server is not None:
                self._server.should_exit = True
            active = tuple(self._active)
            for call in active:
                call.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
            if self._serve_task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(self._serve_task), 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._serve_task.cancel()
                    await asyncio.gather(self._serve_task, return_exceptions=True)
                except Exception:
                    # Startup reports its own exception. Cleanup must still
                    # close the socket and remain safe to await repeatedly.
                    pass
        finally:
            if self._socket is not None:
                with contextlib.suppress(OSError):
                    self._socket.close()
