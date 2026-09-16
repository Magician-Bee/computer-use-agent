"""Lossless, deterministic projection of parent-pinned observation history.

No model, driver, storage or network operations occur here. The manifest is
parent-only: it contains original strings for exact restoration and must never
be appended to the model view. Its hashes establish integrity, not authority;
the caller supplies trusted tool-result hashes and persists the manifest.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import json
import math


CODEC = "computeruse.observation-ref.v1"
REFERENCE_KEY = "$computeruse_observation_ref_v1"
_TOOLS = frozenset({"computer_observe", "computer_act", "computer_finish"})
_FIELDS = ("text", "elements", "tabs")
_MIN_VALUE_CHARS = 192
_MIN_SAVING_CHARS = 32
_MAX_TEXT_CHARS = 10_000
_MAX_TEXT_LINES = 1024
_MAX_TEXT_SOURCES = 16


class ProjectionError(ValueError):
    """Invalid projection data; error text never includes message contents."""


@dataclass(frozen=True)
class Projection:
    messages: list[dict]
    manifest: dict


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ProjectionError("Projection requires finite JSON values and string keys")


def canonical_json(value) -> str:
    """Canonical JSON: UTF-8, sorted keys, compact separators, no NaN/Infinity."""
    try:
        _json_value(value)
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        result.encode("utf-8")
        return result
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ProjectionError("Projection requires valid finite UTF-8 JSON") from None


def canonical_messages(messages: list[dict]) -> str:
    if type(messages) is not list or not all(type(item) is dict for item in messages):
        raise ProjectionError("Projection requires an array of message objects")
    return canonical_json(messages)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message_digest(messages: list[dict]) -> str:
    return _digest(canonical_messages(messages))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("Non-finite number")


def _parse(raw):
    """Parse strict JSON and locate value spans without rewriting whitespace."""
    decoder = json.JSONDecoder(object_pairs_hook=_pairs, parse_constant=_constant)
    value = decoder.decode(raw)
    # JSONDecoder also accepts numeric overflow (1e999) and escaped lone
    # surrogates. Such payloads must remain untouched, not become ref sources.
    canonical_json(value)
    spans = {}

    def whitespace(i):
        while i < len(raw) and raw[i] in " \t\r\n":
            i += 1
        return i

    def walk(i, path):
        start = i = whitespace(i)
        if raw[i] == "{":
            i = whitespace(i + 1)
            while raw[i] != "}":
                key, i = decoder.raw_decode(raw, i)
                i = walk(whitespace(i) + 1, path + (key,))  # colon
                i = whitespace(i)
                if raw[i] == ",":
                    i = whitespace(i + 1)
            i += 1
        elif raw[i] == "[":
            i, index = whitespace(i + 1), 0
            while raw[i] != "]":
                i = whitespace(walk(i, path + (index,)))
                index += 1
                if raw[i] == ",":
                    i = whitespace(i + 1)
            i += 1
        else:
            _, i = decoder.raw_decode(raw, i)
        spans[path] = (start, i)
        return i

    walk(0, ())
    return value, spans


def _pointer(path):
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in path)


def _nodes(value, path):
    yield path, value
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _nodes(value[key], path + (key,))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _nodes(child, path + (i,))


def _has_reference(value):
    if isinstance(value, dict):
        return REFERENCE_KEY in value or any(_has_reference(v) for v in value.values())
    return isinstance(value, list) and any(_has_reference(v) for v in value)


def _eligible(messages, trusted):
    calls, counts = {}, Counter()
    for i, message in enumerate(messages):
        entries = message.get("tool_calls") if message.get("role") == "assistant" else None
        for call in entries if isinstance(entries, list) else []:
            if not isinstance(call, dict) or not isinstance(call.get("id"), str):
                continue
            call_id = call["id"]
            counts[call_id] += 1
            function = call.get("function")
            if (call.get("type") == "function" and isinstance(function, dict)
                    and isinstance(function.get("name"), str) and function["name"] in _TOOLS):
                calls[call_id] = (i, function["name"])
    results = Counter(m.get("tool_call_id") for m in messages
                      if m.get("role") == "tool" and isinstance(m.get("tool_call_id"), str))
    eligible = []
    for i, message in enumerate(messages):
        call_id, raw = message.get("tool_call_id"), message.get("content")
        if (message.get("role") != "tool" or not isinstance(call_id, str) or call_id not in calls
                or counts[call_id] != 1 or results[call_id] != 1 or not isinstance(raw, str)):
            continue
        call_index, name = calls[call_id]
        if call_index >= i or trusted.get(call_id) != _digest(raw) or message.get("name", name) != name:
            continue
        try:
            data, spans = _parse(raw)
            path = () if name == "computer_observe" else ("observation",)
            observation = data if not path else data.get("observation")
            if (not isinstance(observation, dict) or _has_reference(observation)
                    or not isinstance(observation.get("observation_id"), str) or not observation["observation_id"]
                    or type(observation.get("observation_revision")) is not int or observation["observation_revision"] < 1
                    or observation.get("target") not in ("browser", "desktop")
                    or not isinstance(observation.get("text"), str) or not isinstance(observation.get("elements"), list)):
                continue
        except (ValueError, TypeError, AttributeError, RecursionError, IndexError):
            continue
        eligible.append({"index": i, "call": call_id, "raw": raw, "observation": observation,
                         "path": path, "spans": spans})
    return eligible


def _reference(source):
    # This is a data reference, not an instruction or executable tool argument.
    return {REFERENCE_KEY: {"call_id": source["call"], "path": _pointer(source["path"])}}


def _bounded_lines(text):
    if len(text) > _MAX_TEXT_CHARS:
        return None
    lines = text.splitlines(keepends=True)
    return lines if len(lines) <= _MAX_TEXT_LINES else None


def _content_bytes(raw):
    """Cost when raw JSON is embedded as a message content string."""
    return len(canonical_json(raw).encode("utf-8"))


def _line_projection(entry, sources):
    """Choose one bounded, complete source; preserve all unmatched characters."""
    target = entry["observation"]["text"]
    target_lines = _bounded_lines(target)
    if target_lines is None or len(target_lines) < 2 or len(target) < _MIN_VALUE_CHARS:
        return None
    path = entry["path"] + ("text",)
    start, end = entry["spans"][path]
    original_cost = _content_bytes(entry["raw"][start:end])
    best = None
    for source in sources:
        parts, fragments, cursor = [], [], 0
        matcher = SequenceMatcher(None, target_lines, source["lines"], autojunk=False)
        for match in matcher.get_matching_blocks():
            if match.size < 2:
                continue
            fragment = "".join(target_lines[match.a:match.a + match.size])
            if len(fragment) < _MIN_VALUE_CHARS:
                continue
            ref = {"call_id": source["call"], "path": _pointer(source["path"]),
                   "lines": [match.b, match.b + match.size]}
            if _content_bytes(canonical_json(ref)) >= _content_bytes(canonical_json({"literal": fragment})):
                continue
            if cursor < match.a:
                parts.append({"literal": "".join(target_lines[cursor:match.a])})
            parts.append(ref)
            fragments.append({"source_message_index": source["index"],
                "source_tool_call_id": source["call"], "source_path": _pointer(source["path"]),
                "source_value_sha256": source["value_sha256"],
                "line_start": match.b, "line_end": match.b + match.size,
                "fragment_sha256": _digest(canonical_json(fragment))})
            cursor = match.a + match.size
        if not fragments:
            continue
        if cursor < len(target_lines):
            parts.append({"literal": "".join(target_lines[cursor:])})
        raw = canonical_json({REFERENCE_KEY: {"parts": parts}})
        saving = original_cost - _content_bytes(raw)
        if saving < _MIN_SAVING_CHARS or (best is not None and saving <= best[0]):
            continue
        change = {"kind": "line_ranges", "message_index": entry["index"],
                  "tool_call_id": entry["call"], "path": _pointer(path),
                  "value_sha256": _digest(canonical_json(target)), "fragments": fragments}
        best = saving, (start, end, raw), change
    return None if best is None else best[1:]


def project_messages(messages: list[dict], *, trusted_tool_results: Mapping[str, str]) -> Projection:
    """Replace only exact duplicate observation values with explicit references.

    trusted_tool_results maps tool_call_id to SHA256 of the exact UTF-8 tool
    content string. The parent must derive these pins from its own results,
    never from model text. Unpinned, malformed or ambiguous results are untouched.
    Full observation means an intact JSON view, not complete visual coverage;
    any original truncation/uncertainty flags are preserved without reinterpretation.

    Text may also use {REFERENCE_KEY: {"parts": [{"literal": text},
    {"call_id": id, "path": pointer, "lines": [start, end]}, ...]}}. Concatenate
    parts in order; source ranges are zero-based/end-exclusive after
    splitlines(keepends=True). They reference exact multi-line blocks only.
    All value/fragment hashes in the manifest hash canonical_json(value).
    Matching compares at most 16 retained sources, each at most 10,000
    characters and 1,024 lines. Out-of-bound text remains losslessly literal.
    """
    serialized = canonical_messages(messages)
    original = json.loads(serialized)
    if (not isinstance(trusted_tool_results, Mapping)
            or any(type(k) is not str or type(v) is not str or len(v) != 64
                   or any(c not in "0123456789abcdef" for c in v) for k, v in trusted_tool_results.items())):
        raise ProjectionError("Trusted tool-result pins must be call IDs and SHA256 hex digests")
    eligible = _eligible(original, dict(trusted_tool_results))
    projected = json.loads(serialized)
    sources, text_sources, replacements, originals = {}, [], [], {}
    for entry in reversed(eligible):
        candidates = []
        for field in _FIELDS:
            if field in entry["observation"]:
                candidates.extend(_nodes(entry["observation"][field], entry["path"] + (field,)))
        edits, changed_paths = [], []
        for path, value in candidates:
            if any(path[:len(changed)] == changed for changed in changed_paths):
                continue
            start, end = entry["spans"][path]
            if end - start < _MIN_VALUE_CHARS:
                continue
            canonical = canonical_json(value)
            source = sources.get(canonical)
            if source is None:
                continue
            ref = canonical_json(_reference(source))
            if end - start - len(ref) < _MIN_SAVING_CHARS:
                continue
            edits.append((start, end, ref))
            changed_paths.append(path)
            replacements.append({"message_index": entry["index"], "tool_call_id": entry["call"],
                "path": _pointer(path), "source_message_index": source["index"],
                "source_tool_call_id": source["call"], "source_path": _pointer(source["path"]),
                "value_sha256": _digest(canonical)})
        if entry["path"] + ("text",) not in changed_paths:
            line_result = _line_projection(entry, text_sources)
            if line_result is not None:
                edit, change = line_result
                edits.append(edit)
                replacements.append(change)
        if edits:
            raw = entry["raw"]
            originals[str(entry["index"])] = raw
            for start, end, ref in sorted(edits, reverse=True):
                raw = raw[:start] + ref + raw[end:]
            projected[entry["index"]]["content"] = raw
        else:
            # Only unchanged, complete observations become sources. Therefore
            # references never chain, and the newest observation is untouched.
            for path, value in candidates:
                canonical = canonical_json(value)
                source = {"index": entry["index"], "call": entry["call"], "path": path}
                previous = sources.get(canonical)
                if previous is None or len(canonical_json(_reference(source))) < len(canonical_json(_reference(previous))):
                    sources[canonical] = source
            lines = _bounded_lines(entry["observation"]["text"])
            if lines is not None and len(text_sources) < _MAX_TEXT_SOURCES:
                text_sources.append({"index": entry["index"], "call": entry["call"],
                    "path": entry["path"] + ("text",), "lines": lines,
                    "value_sha256": _digest(canonical_json(entry["observation"]["text"]))})
    output = canonical_messages(projected)
    # Escaping the reference in an outer JSON string has overhead too. Refuse
    # a projection that saves raw content chars but expands the actual request.
    if len(output.encode("utf-8")) >= len(serialized.encode("utf-8")):
        projected, output, replacements, originals = original, serialized, [], {}
    manifest = {"version": CODEC, "source_sha256": _digest(serialized), "view_sha256": _digest(output),
        "source_chars": len(serialized), "view_chars": len(output),
        "source_utf8_bytes": len(serialized.encode("utf-8")), "view_utf8_bytes": len(output.encode("utf-8")),
        "trusted_tool_results": {entry["call"]: _digest(entry["raw"]) for entry in eligible},
        "changes": replacements, "original_contents": originals}
    return Projection(projected, manifest)


def restore_messages(projected: list[dict], manifest: dict, *, expected_source_sha256: str | None = None) -> list[dict]:
    """Restore exact original strings and validate the complete deterministic map.

    The caller must trust/pin the parent-only manifest. Supply the independently
    stored source digest to bind restoration to a known original conversation.
    Self-consistent hashes alone do not authenticate an untrusted manifest.
    """
    restored = json.loads(canonical_messages(projected))
    try:
        manifest = json.loads(canonical_json(manifest))
        if manifest.get("version") != CODEC or manifest["view_sha256"] != message_digest(projected):
            raise ValueError
        for index, raw in manifest["original_contents"].items():
            i = int(index)
            if str(i) != index or not 0 <= i < len(restored) or restored[i].get("role") != "tool" or not isinstance(raw, str):
                raise ValueError
            restored[i]["content"] = raw
        digest = message_digest(restored)
        if digest != manifest["source_sha256"] or (expected_source_sha256 is not None and digest != expected_source_sha256):
            raise ValueError
        # Recompute rather than trusting claimed paths, source hashes or edits.
        # This checks every reference against an intact source and also checks
        # that all bytes outside the listed value spans remain unchanged.
        rebuilt = project_messages(restored, trusted_tool_results=manifest["trusted_tool_results"])
        if rebuilt.messages != projected or rebuilt.manifest != manifest:
            raise ValueError
        return restored
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, RecursionError):
        raise ProjectionError("Projection manifest or messages failed integrity verification") from None
