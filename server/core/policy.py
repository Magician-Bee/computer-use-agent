"""Parent-owned browser policy candidate, not an OS or network sandbox.

Only the parent registers policies and pins executor ToolManifests. A planner
may propose an Action, never an authoritative policy, manifest, scope or lease.
This module has no model calls, store mutations, or driver operations. The
gateway must separately enforce current leases/observations/approvals and apply
origin restrictions to browser redirects, clicks and requests at execution.
"""
from __future__ import annotations

import hashlib
import threading
from typing import Annotated, get_args
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import Field, ValidationError, model_validator

from server.schemas import Action
from .contracts import (ActionEnvelope, Contract, Identifier, ResourceScope,
                        Sha256, ToolManifest, canonical_json)
from .store import AdmissionDenied, Conflict

_ACTION_TYPES = frozenset(get_args(Action.model_fields["type"].annotation))
_PARENT_ONLY = frozenset({"done", "ask_user"})
_NO_INPUT_EFFECT = _PARENT_ONLY | {"wait"}
_BROWSER_ACTION_TYPES = _ACTION_TYPES - _PARENT_ONLY - {"open_app"}


class ExecutionPolicy(Contract):
    policy_id: Identifier
    resource_scopes: Annotated[tuple[ResourceScope, ...], Field(min_length=1, max_length=100)]
    allowed_action_types: Annotated[tuple[str, ...], Field(max_length=50)]
    approval_action_types: Annotated[tuple[str, ...], Field(max_length=50)] | None = None

    @model_validator(mode="after")
    def normalize_capabilities(self):
        allowed = self.allowed_action_types
        required = self.approval_action_types
        if len(allowed) != len(set(allowed)) or not set(allowed) <= _ACTION_TYPES:
            raise ValueError("allowed_action_types must contain unique existing Action types")
        if required is None:
            required = tuple(kind for kind in allowed if kind not in _NO_INPUT_EFFECT)
        if len(required) != len(set(required)) or not set(required) <= set(allowed):
            raise ValueError("approval_action_types must be a unique subset of allowed_action_types")
        scopes = {canonical_json(scope.model_dump(mode="json")): scope for scope in self.resource_scopes}
        if len(scopes) != len(self.resource_scopes):
            raise ValueError("resource scopes must be unique")
        resource_ids = [scope.resource_id for scope in self.resource_scopes]
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("one policy cannot assign different scopes to the same resource ID")
        object.__setattr__(self, "resource_scopes", tuple(scopes[key] for key in sorted(scopes)))
        object.__setattr__(self, "allowed_action_types", tuple(sorted(allowed)))
        object.__setattr__(self, "approval_action_types", tuple(sorted(required)))
        return self

    @property
    def digest(self) -> str:
        payload = canonical_json(self.model_dump(mode="json"))
        return hashlib.sha256(("computeruse.execution-policy.v1\0" + payload).encode("utf-8")).hexdigest()


class PolicyDecision(Contract):
    require_approval: Annotated[bool, Field(strict=True)]
    policy_digest: Sha256


def origin_identity(value: str) -> tuple[str, str, int]:
    """Browser same-origin identity without DNS or a URL-parser ambiguity."""
    try:
        if (not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value)
                or "\\" in value):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError
        host = parsed.hostname
        if "%" in host or "*" in host:
            raise ValueError
        # Normalize DNS hostnames as a browser does; IPv6 stays literal.
        host = host.encode("idna").decode("ascii").lower() if ":" not in host else host.lower()
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        return parsed.scheme, host, port
    except (TypeError, ValueError, UnicodeError):
        raise AdmissionDenied("Navigation URL has no unambiguous HTTP(S) origin") from None


class PolicyRegistry:
    """Immutable policy IDs; the gateway owns the sole executor manifest registry."""

    def __init__(self):
        self._policies: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(self, policy: ExecutionPolicy) -> None:
        if not isinstance(policy, ExecutionPolicy):
            raise AdmissionDenied("Only a parent-created ExecutionPolicy may be registered")
        # Snapshot, never retain a caller's mutable model or unchecked copy.
        try:
            policy = ExecutionPolicy.model_validate_json(policy.model_dump_json())
        except (ValidationError, TypeError, ValueError):
            raise AdmissionDenied("Policy registration failed validation") from None
        if any(scope.kind != "browser" for scope in policy.resource_scopes):
            raise AdmissionDenied("This candidate policy registry only supports browser resources")
        serialized = canonical_json(policy.model_dump(mode="json"))
        with self._lock:
            previous = self._policies.get(policy.policy_id)
            if previous is not None and previous != serialized:
                raise Conflict("A policy ID is already bound to different immutable content")
            self._policies[policy.policy_id] = serialized

    def digest_for(self, policy_id: str) -> str:
        """Retrieve the immutable snapshot digest for the parent's durable pin."""
        with self._lock:
            serialized = self._policies.get(policy_id)
        if serialized is None:
            raise AdmissionDenied("Policy ID has not been registered by the parent")
        return ExecutionPolicy.model_validate_json(serialized).digest

    def evaluate(self, envelope: ActionEnvelope, manifest: ToolManifest) -> PolicyDecision:
        """Evaluate parent-pinned inputs; no policy or manifest comes from a model."""
        if not isinstance(envelope, ActionEnvelope) or not isinstance(manifest, ToolManifest):
            raise AdmissionDenied("Policy evaluation requires validated parent contract objects")
        try:
            envelope = ActionEnvelope.model_validate_json(envelope.model_dump_json())
            manifest = ToolManifest.model_validate_json(manifest.model_dump_json())
        except (ValidationError, TypeError, ValueError):
            raise AdmissionDenied("Action or registered tool manifest failed validation") from None
        if envelope.resource_scope.kind != "browser":
            raise AdmissionDenied("Native desktop and other resource kinds are not enabled by this browser candidate")
        with self._lock:
            serialized = self._policies.get(envelope.policy_ref)
        if serialized is None:
            raise AdmissionDenied("Action references an unregistered parent policy")
        policy = ExecutionPolicy.model_validate_json(serialized)
        if envelope.resource_scope not in policy.resource_scopes:
            raise AdmissionDenied("Action resource scope does not exactly match its policy")
        kind = envelope.action.type
        if kind not in _BROWSER_ACTION_TYPES or kind not in policy.allowed_action_types:
            raise AdmissionDenied("Action type is not enabled through this execution policy")
        if (manifest.tool_id != envelope.tool_id or manifest.version != envelope.tool_version
                or kind not in manifest.action_types or envelope.resource_scope not in manifest.resource_scopes):
            raise AdmissionDenied("Registered tool identity, version or capability scope does not match this action")
        if not manifest.integrity.verified:
            raise AdmissionDenied("Registered tool integrity has not been verified")
        try:
            typed_args = envelope.action.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
            Draft202012Validator(manifest.input_schema, format_checker=FormatChecker()).validate(typed_args)
        except Exception:
            # Do not include validator paths/input that may contain private text.
            raise AdmissionDenied("Action arguments do not satisfy the registered tool schema") from None
        expected = envelope.resource_scope.website_origin
        if kind == "new_tab" and expected is not None and not envelope.action.url:
            raise AdmissionDenied("A tab in an origin-restricted resource requires an explicit same-origin URL")
        if kind in {"navigate", "new_tab"} and envelope.action.url:
            actual = origin_identity(envelope.action.url)
            if expected is not None and actual != origin_identity(expected):
                raise AdmissionDenied("Navigation destination is outside the exact authorized origin")
        return PolicyDecision(require_approval=kind in policy.approval_action_types, policy_digest=policy.digest)
