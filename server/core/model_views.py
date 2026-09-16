"""Opt-in, lossless model-input projection in the sole parent TaskStore.

This is data transport, not a second conversation or authority loop. The raw
diagnostic fixture history and reversible view are committed before inference.
Production retention/consent UI is not implemented; default model endpoints do
not enable this facility. It never drops changed or cross-page observations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
import zlib

from server.context_projection import canonical_messages, project_messages, restore_messages
from server.model_server import PreparedModelRequest
from .store import AdmissionDenied, Conflict, StoreError, TaskStore

MAX_JSON_BYTES = 2_097_152
MAX_MANIFEST_BYTES = 8_388_608
_TOOLS = frozenset({"computer_observe", "computer_act", "computer_finish", "computer_ask_user"})


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise StoreError("Model view requires finite JSON") from None


def _encode(text, limit=MAX_JSON_BYTES):
    try:
        raw = text.encode("utf-8")
    except (AttributeError, UnicodeError):
        raise StoreError("Model view must be valid UTF-8") from None
    if len(raw) > limit:
        raise AdmissionDenied("Model view exceeds retained document limit")
    return zlib.compress(raw, level=6)


def _decode(blob, limit=MAX_JSON_BYTES):
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(blob, limit+1)
        if len(raw) > limit or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError
        return raw.decode("utf-8")
    except (ValueError, UnicodeError, zlib.error, TypeError):
        raise StoreError("Stored model view failed integrity checks") from None


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelViewSession:
    """One task's bounded diagnostic retention and authenticated tool history.

    Construct only with explicit synthetic-fixture retention. All tables and
    events share the supplied TaskStore connection and transaction boundary.
    The worker/model cannot configure retention, pin results, or write a view.
    """

    def __init__(self, store: TaskStore, task_id: str, *, retention_mode: str,
                 max_retained_bytes: int = 16_777_216):
        if retention_mode != "diagnostic_fixture":
            raise AdmissionDenied("Model view retention requires explicit diagnostic fixture mode")
        if type(max_retained_bytes) is not int or not 1024 <= max_retained_bytes <= 134_217_728:
            raise ValueError("Model view retention budget must be 1 KiB to 128 MiB")
        self.store, self.task_id = store, task_id
        with store._tx() as db:
            task = store._task(db, task_id)
            row = db.execute("SELECT mode,max_bytes FROM model_view_retention WHERE task_id=?", (task_id,)).fetchone()
            if row:
                if row["mode"] != retention_mode or row["max_bytes"] != max_retained_bytes:
                    raise Conflict("Model view retention cannot silently change across sessions")
            else:
                if task.status not in {"DRAFT", "QUEUED", "PLANNING", "READY", "RUNNING"}:
                    raise AdmissionDenied("Task cannot enable diagnostic retention in its current state")
                db.execute("INSERT INTO model_view_retention VALUES(?,?,?)", (task_id, retention_mode, max_retained_bytes))
                store._event(db, task, "model_view.retention_enabled", {"mode": retention_mode, "max_bytes": max_retained_bytes})

    def _check_budget(self, db, additional):
        policy = db.execute("SELECT max_bytes FROM model_view_retention WHERE task_id=?", (self.task_id,)).fetchone()
        used = sum(db.execute(f"SELECT COALESCE(SUM(stored_bytes),0) FROM {table} WHERE task_id=?",
                              (self.task_id,)).fetchone()[0]
                   for table in ("model_tool_results", "model_request_views"))
        if policy is None or used + additional > policy["max_bytes"]:
            raise AdmissionDenied("Model view retention budget is exhausted; no inference was admitted")

    def register_tool_result(self, name: str, result: dict):
        """Pin an actual parent-returned result before giving it to Hermes.

        Match the worker's tool-result JSON serialization exactly, including
        key order and spaces. This pin contains no model-proposed authority.
        """
        if name not in _TOOLS or type(result) is not dict:
            raise AdmissionDenied("Only parent ComputerTools results can be retained")
        try:
            content = json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise StoreError("Tool result requires finite JSON") from None
        if len(content) > 24000:
            raise AdmissionDenied("Tool result exceeds the complete Hermes result boundary")
        blob, digest = _encode(content, 96000), _sha(content)
        with self.store._tx() as db:
            task = self.store._task(db, self.task_id)
            old = db.execute("SELECT content FROM model_tool_results WHERE task_id=? AND tool_name=? AND digest=?",
                             (task.id, name, digest)).fetchone()
            if old:
                if _decode(old["content"], 96000) != content:
                    raise StoreError("Pinned tool result failed integrity checks")
                return digest
            self._check_budget(db, len(blob))
            db.execute("INSERT INTO model_tool_results VALUES(?,?,?,?,?)", (task.id, name, digest, blob, len(blob)))
            self.store._event(db, task, "model_view.tool_result_retained", {"tool_name": name, "sha256": digest})
        return digest

    def _trusted_results(self, messages):
        with self.store._tx() as db:
            pinned = set()
            for row in db.execute("SELECT tool_name,digest,content FROM model_tool_results WHERE task_id=?", (self.task_id,)):
                if row["tool_name"] not in _TOOLS or _sha(_decode(row["content"], 96000)) != row["digest"]:
                    raise StoreError("Pinned tool result failed integrity checks")
                pinned.add((row["tool_name"], row["digest"]))
        calls = {}
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "assistant":
                entries = message.get("tool_calls")
                for call in entries if isinstance(entries, list) else []:
                    if (isinstance(call, dict) and isinstance(call.get("id"), str)
                            and isinstance(call.get("function"), dict)
                            and isinstance(call["function"].get("name"), str)):
                        calls.setdefault(call["id"], []).append(call["function"]["name"])
        trusted = {}
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            identifier = message.get("tool_call_id")
            if not isinstance(identifier, str):
                continue
            names = calls.get(identifier, [])
            digest = _sha(message["content"])
            if len(names) == 1 and (names[0], digest) in pinned:
                trusted[identifier] = digest
        return trusted

    async def prepare_request(self, payload: dict) -> PreparedModelRequest:
        """Persist source + reversible view atomically, then permit one API call."""
        task = self.store.get_task(self.task_id)
        if task.status not in {"RUNNING", "SUCCEEDED", "WAITING_USER"}:
            raise AdmissionDenied("Task state does not permit a model request")
        messages = payload.get("messages")
        if type(messages) is not list:
            raise AdmissionDenied("Model request requires a messages list")
        original = canonical_messages(messages)
        source_blob = _encode(original)
        projection = project_messages(messages, trusted_tool_results=self._trusted_results(messages))
        if restore_messages(projection.messages, projection.manifest, expected_source_sha256=_sha(original)) != messages:
            raise StoreError("Model view did not restore the original history")
        projected = canonical_messages(projection.messages)
        audit = {"id": uuid.uuid4().hex, "version": projection.manifest["version"],
            "source_sha256": _sha(original), "view_sha256": _sha(projected),
            "source_chars": len(original), "view_chars": len(projected), "changed": original != projected}
        blobs = (source_blob, _encode(projected), _encode(_json(projection.manifest), MAX_MANIFEST_BYTES))
        with self.store._tx() as db:
            current = self.store._task(db, task.id, task.revision)
            if current.status not in {"RUNNING", "SUCCEEDED", "WAITING_USER"}:
                raise AdmissionDenied("Task stopped or paused before model view admission")
            self._check_budget(db, sum(map(len, blobs)))
            db.execute("INSERT INTO model_request_views VALUES(?,?,?,?,?,?,?,?)",
                (audit["id"], task.id, task.revision, _json(audit), *blobs, sum(map(len, blobs))))
            self.store._event(db, current, "model_view.prepared", audit)
        prepared = copy.deepcopy(payload)
        prepared["messages"] = projection.messages
        return PreparedModelRequest(payload=prepared, audit=audit)

    def read_view(self, identifier: str):
        """Scoped readback with integrity and exact lossless restoration checks."""
        with self.store._tx() as db:
            row = db.execute("SELECT * FROM model_request_views WHERE id=? AND task_id=?",
                             (identifier, self.task_id)).fetchone()
            if row is None:
                raise StoreError("Retained model view does not exist for this task")
        source_text, view_text = _decode(row["source"]), _decode(row["view"])
        try:
            audit = json.loads(row["audit"])
            source, view = json.loads(source_text), json.loads(view_text)
            manifest = json.loads(_decode(row["manifest"], MAX_MANIFEST_BYTES))
            expected_audit = {"id": identifier, "version": manifest["version"],
                "source_sha256": _sha(source_text), "view_sha256": _sha(view_text),
                "source_chars": len(source_text), "view_chars": len(view_text), "changed": source_text != view_text}
            if (_json(audit) != _json(expected_audit)
                    or restore_messages(view, manifest, expected_source_sha256=expected_audit["source_sha256"]) != source):
                raise ValueError
        except (ValueError, KeyError, TypeError, RecursionError):
            raise StoreError("Retained model view failed integrity checks") from None
        return {"audit": audit, "source_messages": source, "view_messages": view, "manifest": manifest}
