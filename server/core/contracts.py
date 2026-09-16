"""Version 1.0 shared data contracts (partial P00/P01/P08-P10 work).

Contracts validate structure and consistency, not the truth of evidence or the
identity of a caller. The sole TaskStore/ActionGateway must check current state,
policy, lease fencing, authentic approvals, and verifier evidence. There is no
state transition engine, persistence, tool invocation, or planner in this file.

Action is imported unchanged from server.schemas. Hashes bind its full normalized
payload (including defaults), not a second action definition. Store boundaries
must revalidate serialized data: Pydantic model_copy and nested mutable objects
are not security boundaries, even on a frozen outer model.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator
from pydantic import (AwareDatetime, BaseModel, ConfigDict, Field, JsonValue,
                      StringConstraints, field_validator, model_validator)

from server.schemas import Action

SCHEMA_VERSION = "1.0"
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=8000)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=0)]
Fence = Annotated[int, Field(strict=True, ge=1)]
Nonnegative = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]
TaskStatus = Literal["DRAFT", "QUEUED", "PLANNING", "READY", "RUNNING", "VERIFYING",
    "SUCCEEDED", "FAILED", "INCONCLUSIVE", "WAITING_APPROVAL", "WAITING_USER", "PAUSED",
    "REOBSERVE", "BLOCKED", "REPLAN", "RECONCILING", "CANCELLING", "CANCELLED"]
StepState = Literal["pending", "ready", "running", "accepted", "verified", "failed", "inconclusive", "cancelled"]
ResourceKind = Literal["browser", "desktop", "filesystem", "model", "worker", "terminal"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_json(value: Any) -> str:
    """v1 Python normalized JSON; not a claim of RFC 8785 cross-language JCS."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _unique(values, description: str):
    if len(values) != len(set(values)):
        raise ValueError(f"{description} must be unique")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True,
                              revalidate_instances="always", allow_inf_nan=False, hide_input_in_errors=True)
    schema_version: Literal["1.0"] = SCHEMA_VERSION

    @field_validator("*", mode="after")
    @classmethod
    def reject_blank_and_normalize_timestamp(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("text must not be blank")
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc)
        return value


class ResourceScope(Contract):
    resource_id: Identifier
    kind: ResourceKind
    device_id: Identifier | None = None
    session_id: Identifier | None = None
    window_id: Identifier | Annotated[int, Field(strict=True, gt=0)] | None = None
    pid: Annotated[int, Field(strict=True, gt=0)] | None = None
    website_origin: Annotated[str, StringConstraints(max_length=4096)] | None = None
    path: Annotated[str, StringConstraints(max_length=4096)] | None = None
    account_id: Identifier | None = None

    @field_validator("window_id", mode="before")
    @classmethod
    def valid_window_id(cls, value):
        if isinstance(value, bool) or (isinstance(value, int) and value <= 0):
            raise ValueError("window_id must be a positive integer or explicit identifier")
        return value

    @field_validator("website_origin")
    @classmethod
    def exact_origin(cls, value):
        if value is None:
            return value
        try:
            parsed = urlsplit(value)
            _ = parsed.port
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.query or parsed.fragment
                    or parsed.path not in {"", "/"} or any(c in value for c in "*?\n\r\t")):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("website_origin must be one exact HTTP(S) origin without credentials or a path") from None
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @field_validator("path")
    @classmethod
    def exact_absolute_path(cls, value):
        if value is None:
            return value
        if (not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())
                or any(c in value for c in "*?\0") or ".." in PureWindowsPath(value).parts
                or ".." in PurePosixPath(value).parts):
            raise ValueError("path must be explicit and absolute without wildcards or parent traversal")
        return value

    @model_validator(mode="after")
    def required_resource_identity(self):
        required = {"browser": ("session_id",), "terminal": ("session_id",),
            "desktop": ("device_id", "session_id", "window_id", "pid"), "filesystem": ("path",),
            "model": ("website_origin",), "worker": ("device_id",)}[self.kind]
        if any(getattr(self, name) is None for name in required):
            raise ValueError("resource scope lacks required identity for its kind")
        return self


class Budget(Contract):
    max_steps: Revision = 30
    max_seconds: Nonnegative | None = None
    max_tokens: Revision | None = None
    max_cost_microunits: Revision | None = None
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] | None = None

    @model_validator(mode="after")
    def explicit_cost_units(self):
        if (self.max_cost_microunits is None) != (self.currency is None):
            raise ValueError("cost budget and currency must be specified together")
        return self


class SuccessCriterion(Contract):
    id: Identifier
    description: Text
    verifier_ref: Identifier | None = None


class CompletionVerdict(Contract):
    actor: Literal["external_verifier"] = "external_verifier"
    verifier_ref: Identifier
    verdict: Literal["succeeded", "failed", "inconclusive"]
    task_revision: Revision
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=1000)]
    criterion_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=200)]
    decided_at: AwareDatetime

    @model_validator(mode="after")
    def unique_references(self):
        _unique(self.evidence_ids, "evidence IDs")
        _unique(self.criterion_ids, "criterion IDs")
        return self


class Task(Contract):
    id: Identifier
    revision: Revision = 0
    goal: Text
    constraints: Annotated[tuple[Text, ...], Field(max_length=200)] = ()
    success_criteria: Annotated[tuple[SuccessCriterion, ...], Field(min_length=1, max_length=200)]
    plan_version: Revision = 0
    mode: Literal["local_only", "local_first", "cloud_authorized"] = "local_only"
    budget: Budget = Field(default_factory=Budget)
    policy_ref: Identifier
    status: TaskStatus = "DRAFT"
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    completion: CompletionVerdict | None = None

    @model_validator(mode="after")
    def terminal_evidence_contract(self):
        _unique([criterion.id for criterion in self.success_criteria], "success criterion IDs")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.completion is not None:
            expected_status = {"succeeded": "SUCCEEDED", "failed": "FAILED", "inconclusive": "INCONCLUSIVE"}[self.completion.verdict]
            if self.status != expected_status or self.completion.task_revision != self.revision:
                raise ValueError("completion verdict must match the task's final status and revision")
            if not set(self.completion.criterion_ids) <= {criterion.id for criterion in self.success_criteria}:
                raise ValueError("completion references an unknown success criterion")
        if self.status == "SUCCEEDED":
            verdict = self.completion
            if (verdict is None or verdict.verdict != "succeeded" or verdict.task_revision != self.revision
                    or set(verdict.criterion_ids) != {criterion.id for criterion in self.success_criteria}):
                raise ValueError("SUCCEEDED requires a matching external verifier verdict covering every success criterion")
        elif self.completion is not None and self.completion.verdict == "succeeded":
            raise ValueError("a successful completion verdict requires SUCCEEDED status")
        return self


class Step(Contract):
    id: Identifier
    task_id: Identifier
    task_revision: Revision
    plan_version: Revision
    dependencies: Annotated[tuple[Identifier, ...], Field(max_length=1000)] = ()
    preconditions: Annotated[tuple[Text, ...], Field(max_length=200)] = ()
    expected_effect: Text
    verifier_ref: Identifier
    worker_ref: Identifier | None = None
    state: StepState = "pending"
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=1000)] = ()

    @model_validator(mode="after")
    def step_consistency(self):
        _unique(self.dependencies, "dependency IDs")
        _unique(self.evidence_ids, "evidence IDs")
        if self.id in self.dependencies:
            raise ValueError("a step cannot depend on itself")
        if self.state == "verified" and not self.evidence_ids:
            raise ValueError("verified steps require evidence references")
        return self


class ObservationRef(Contract):
    id: Identifier
    task_id: Identifier
    task_revision: Revision
    revision: Revision
    observed_at: AwareDatetime
    expires_at: AwareDatetime
    resource_scope: ResourceScope
    frame_version: Revision
    coordinate_space: Literal["screenshot_pixels", "document", "none"]
    image_ref: Annotated[str, StringConstraints(min_length=1, max_length=4096)] | None = None
    image_digest: Sha256 | None = None

    @model_validator(mode="after")
    def valid_observation_lifetime(self):
        if self.expires_at <= self.observed_at:
            raise ValueError("observation expiry must follow capture time")
        if self.image_digest is not None and self.image_ref is None:
            raise ValueError("an image digest requires an image reference")
        return self


class ActionEnvelope(Contract):
    action_id: Identifier
    task_id: Identifier
    task_revision: Revision
    step_id: Identifier
    tool_id: Identifier
    tool_version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    action: Action
    observation_id: Identifier
    observation_revision: Revision
    policy_ref: Identifier
    resource_scope: ResourceScope
    approval_id: Identifier | None = None
    lease_id: Identifier
    fencing_token: Fence
    idempotency_key: Identifier
    payload_sha256: Sha256
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("action", mode="before")
    @classmethod
    def revalidate_existing_action(cls, value):
        # Action's own model_config does not revalidate existing instances.
        return value.model_dump(mode="json") if isinstance(value, Action) else value

    def execution_payload(self) -> dict:
        # No circular approval hash. Audit creation time is not an operation.
        return self.model_dump(mode="json", exclude={"payload_sha256", "approval_id", "created_at"})

    def compute_payload_hash(self) -> str:
        data = ("computeruse.action-envelope.v1\0" + canonical_json(self.execution_payload())).encode("utf-8")
        return hashlib.sha256(data).hexdigest()

    def verify_payload_hash(self) -> bool:
        try:
            return hmac.compare_digest(self.payload_sha256, self.compute_payload_hash())
        except (ValueError, TypeError, OverflowError):
            return False

    @model_validator(mode="after")
    def payload_matches_hash(self):
        if not self.verify_payload_hash():
            raise ValueError("action payload_sha256 does not match its exact execution payload")
        return self

    @classmethod
    def create(cls, **fields) -> ActionEnvelope:
        """Normalize with the same field validators before creating a digest."""
        if "payload_sha256" in fields:
            raise ValueError("create computes payload_sha256; do not supply one")
        # Normalize each declared field before hashing, then validate the full
        # object including cross-field rules. No unchecked model_construct().
        from pydantic import TypeAdapter
        normalized = {}
        for name, field in cls.model_fields.items():
            if name == "payload_sha256":
                continue
            if name in fields:
                value = fields[name]
                if name == "action" and isinstance(value, Action):
                    value = value.model_dump(mode="json")
                normalized[name] = TypeAdapter(field.rebuild_annotation()).validate_python(value)
            elif not field.is_required():
                normalized[name] = field.get_default(call_default_factory=True)
        extra = set(fields) - set(cls.model_fields)
        if extra:
            raise ValueError("unknown envelope fields")
        # Serialization through field values preserves normalized defaults.
        serialized = {name: value.model_dump(mode="json") if isinstance(value, BaseModel)
                      else value for name, value in normalized.items()
                      if name not in {"approval_id", "created_at"}}
        digest = hashlib.sha256(("computeruse.action-envelope.v1\0" + canonical_json(serialized)).encode("utf-8")).hexdigest()
        return cls.model_validate({**normalized, "payload_sha256": digest})


class Approval(Contract):
    id: Identifier
    task_id: Identifier
    task_revision: Revision
    action_id: Identifier
    payload_sha256: Sha256
    resource_scope: ResourceScope
    principal: Identifier
    destination: Text
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    revoked: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def valid_approval_lifetime(self):
        if self.expires_at <= self.issued_at:
            raise ValueError("approval expiry must follow issue time")
        return self

    def matches(self, envelope: ActionEnvelope, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        return (not self.revoked and self.issued_at <= now < self.expires_at
            and envelope.verify_payload_hash() and self.task_id == envelope.task_id
            and self.task_revision == envelope.task_revision and self.action_id == envelope.action_id
            and hmac.compare_digest(self.payload_sha256, envelope.payload_sha256)
            and self.resource_scope == envelope.resource_scope
            and envelope.approval_id == self.id)


class ResourceLease(Contract):
    id: Identifier
    resource_id: Identifier
    resource_scope: ResourceScope
    owner: Identifier
    fencing_token: Fence
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    revoked: Annotated[bool, Field(strict=True)] = False

    @model_validator(mode="after")
    def lease_consistency(self):
        if self.resource_id != self.resource_scope.resource_id:
            raise ValueError("lease resource_id differs from its exact resource scope")
        if self.expires_at <= self.issued_at:
            raise ValueError("lease expiry must follow issue time")
        return self


class RetentionPolicy(Contract):
    mode: Literal["summary_only", "redacted_artifact", "raw_artifact"] = "summary_only"
    expires_at: AwareDatetime | None = None


class Evidence(Contract):
    id: Identifier
    task_id: Identifier
    task_revision: Revision
    observation_id: Identifier
    observation_revision: Revision
    source: Literal["external_verifier", "observer", "tool", "user", "planner"]
    observed_at: AwareDatetime
    check_type: Identifier
    result: Literal["pass", "fail", "inconclusive"]
    summary: Text
    verifier_ref: Identifier | None = None
    criterion_ids: Annotated[tuple[Identifier, ...], Field(max_length=200)] = ()
    redaction: Literal["not_applicable", "redacted", "contains_sensitive"] = "not_applicable"
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    optional_digest: Sha256 | None = None

    @model_validator(mode="after")
    def explicit_verifier_source(self):
        _unique(self.criterion_ids, "criterion IDs")
        if self.source == "external_verifier" and not self.verifier_ref:
            raise ValueError("external verifier evidence requires verifier_ref")
        return self


class Event(Contract):
    id: Identifier
    sequence: Revision
    timestamp: AwareDatetime
    task_id: Identifier
    task_revision: Revision
    step_id: Identifier | None = None
    type: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.-]{0,99}$")]
    redacted_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("redacted_payload")
    @classmethod
    def bounded_json_payload(cls, value):
        if len(canonical_json(value).encode("utf-8")) > 32768:
            raise ValueError("event payload exceeds 32 KiB")
        return value


class PendingEffect(Contract):
    action_id: Identifier
    payload_sha256: Sha256
    state: Literal["dispatched", "outcome_unknown", "accepted_unverified"]
    idempotency_key: Identifier


class ResumeRules(Contract):
    reobserve_required: Annotated[bool, Field(strict=True)] = True
    reconcile_required: Annotated[bool, Field(strict=True)] = True
    replay_pending_actions: Literal["never"] = "never"

    @model_validator(mode="after")
    def no_blind_replay(self):
        if not self.reobserve_required or not self.reconcile_required:
            raise ValueError("resume must reobserve and reconcile pending effects")
        return self


class Checkpoint(Contract):
    id: Identifier
    task_id: Identifier
    task_revision: Revision
    plan_version: Revision
    created_at: AwareDatetime = Field(default_factory=utc_now)
    verified_evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=1000)] = ()
    pending_effects: Annotated[tuple[PendingEffect, ...], Field(max_length=1000)] = ()
    budget_remaining: Budget
    policy_digest: Sha256
    resume_rules: ResumeRules = Field(default_factory=ResumeRules)

    @model_validator(mode="after")
    def unique_checkpoint_references(self):
        _unique(self.verified_evidence_ids, "verified evidence IDs")
        _unique([effect.action_id for effect in self.pending_effects], "pending action IDs")
        return self


class ToolProvenance(Contract):
    source: Text
    version_ref: Text | None = None
    inspected_at: AwareDatetime | None = None


class ToolIntegrity(Contract):
    algorithm: Literal["sha256"] = "sha256"
    expected_sha256: Sha256
    observed_sha256: Sha256 | None = None
    verified_at: AwareDatetime | None = None
    verifier_ref: Identifier | None = None

    @model_validator(mode="after")
    def integrity_claim_has_measurement(self):
        if self.verified_at is not None and (self.observed_sha256 is None or self.verifier_ref is None):
            raise ValueError("integrity verification requires an observed digest and verifier identity")
        return self

    @property
    def verified(self) -> bool:
        return (self.observed_sha256 == self.expected_sha256 and self.verified_at is not None
                and self.verifier_ref is not None)


class ToolManifest(Contract):
    tool_id: Identifier
    version: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    action_types: Annotated[tuple[str, ...], Field(max_length=50)] = ()
    input_schema: dict[str, JsonValue]
    resource_scopes: Annotated[tuple[ResourceScope, ...], Field(max_length=100)] = ()
    permissions: Annotated[tuple[Identifier, ...], Field(max_length=100)] = ()
    side_effects: Literal["none", "reversible", "irreversible", "unknown"] = "unknown"
    sandbox_profile: Identifier
    provenance: ToolProvenance
    integrity: ToolIntegrity

    @field_validator("action_types")
    @classmethod
    def existing_action_types_only(cls, value):
        _unique(value, "action types")
        if not set(value) <= set(get_args(Action.model_fields["type"].annotation)):
            raise ValueError("manifest contains an unknown Action type")
        return value

    @field_validator("input_schema")
    @classmethod
    def valid_input_schema(cls, value):
        canonical_json(value)
        def local_references_only(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                        raise ValueError("tool schema references must be local; validation must not fetch remote schemas")
                    local_references_only(child)
            elif isinstance(node, list):
                for child in node:
                    local_references_only(child)
        local_references_only(value)
        try:
            Draft202012Validator.check_schema(value)
        except Exception:
            raise ValueError("tool input_schema is not valid JSON Schema") from None
        return value
