"""Contract boundaries only: no store, model request, browser, or desktop input."""
from datetime import datetime, timedelta, timezone
import json

from pydantic import ValidationError
import pytest

from server.schemas import Action
from server.core.contracts import (ActionEnvelope, Approval, Budget, Checkpoint,
    CompletionVerdict, Evidence, Event, ObservationRef, PendingEffect, ResourceLease,
    ResourceScope, ResumeRules, Step, SuccessCriterion, Task, ToolManifest)

NOW = datetime(2026, 9, 11, 8, tzinfo=timezone(timedelta(hours=8)))
DIGEST = "a" * 64


@pytest.fixture
def scope():
    return ResourceScope(resource_id="browser:one", kind="browser", session_id="session1",
                         website_origin="http://127.0.0.1:8080/")


@pytest.fixture
def envelope_fields(scope):
    return dict(action_id="action1", task_id="task1", task_revision=3, step_id="step1",
        tool_id="computer_action", tool_version="1.0.0", action=Action(type="type", target="e1", text="你好"),
        observation_id="obs1", observation_revision=9, resource_scope=scope,
        policy_ref="policy1", lease_id="lease1", fencing_token=2,
        idempotency_key="operation1", created_at=NOW)


@pytest.fixture
def envelope(envelope_fields):
    return ActionEnvelope.create(**envelope_fields)


@pytest.fixture
def task():
    return Task(id="task1", revision=3, goal="Save the requested profile", policy_ref="policy1",
        success_criteria=[SuccessCriterion(id="saved", description="Saved data exactly matches the requested values")],
        created_at=NOW, updated_at=NOW)


def approval_for(envelope, **changes):
    return Approval(**(dict(id="approval1", task_id=envelope.task_id, task_revision=envelope.task_revision,
        action_id=envelope.action_id, payload_sha256=envelope.payload_sha256,
        resource_scope=envelope.resource_scope, principal="user1", destination="local fixture profile",
        issued_at=NOW, expires_at=NOW + timedelta(minutes=5)) | changes))


def verdict_for(task, **changes):
    return CompletionVerdict(**(dict(verifier_ref="fixture_verifier", verdict="succeeded",
        task_revision=task.revision, evidence_ids=["evidence1"], criterion_ids=["saved"], decided_at=NOW) | changes))


def test_shared_action_is_reused_and_exact_payload_survives_json_round_trip(envelope):
    assert type(envelope.action) is Action
    assert len(envelope.payload_sha256) == 64
    assert envelope.verify_payload_hash()
    serialized = envelope.model_dump_json()
    assert ActionEnvelope.model_validate_json(serialized) == envelope
    assert ActionEnvelope.model_validate(json.loads(serialized)) == envelope
    assert envelope.created_at.utcoffset() == timedelta(0)
    assert envelope.resource_scope.website_origin == "http://127.0.0.1:8080"


def test_hash_normalizes_defaults_and_json_property_order(envelope_fields, envelope):
    plain = envelope_fields | {"action": {"text": "你好", "type": "type", "target": "e1"}}
    assert ActionEnvelope.create(**plain).payload_sha256 == envelope.payload_sha256
    assert ActionEnvelope.create(**(plain | {"action": envelope.action.model_dump()})).payload_sha256 == envelope.payload_sha256


@pytest.mark.parametrize("field,value", [
    ("action_id", "another_action"), ("task_id", "another_task"), ("task_revision", 4),
    ("tool_id", "another_tool"), ("tool_version", "2.0.0"), ("observation_id", "obs2"),
    ("observation_revision", 10), ("policy_ref", "policy2"), ("lease_id", "lease2"),
    ("fencing_token", 3), ("idempotency_key", "another_operation"),
])
def test_execution_identity_change_invalidates_digest(envelope, field, value):
    with pytest.raises(ValidationError, match="payload_sha256"):
        ActionEnvelope.model_validate(envelope.model_dump() | {field: value})


@pytest.mark.parametrize("field,value", [("text", "different content"), ("target", "e2"),
    ("reason", "different public purpose"), ("button", "right")])
def test_all_action_fields_are_hashed_not_just_type_and_target(envelope, field, value):
    data = envelope.model_dump()
    data["action"][field] = value
    with pytest.raises(ValidationError, match="payload_sha256"):
        ActionEnvelope.model_validate(data)


def test_resource_and_account_changes_are_bound(envelope):
    data = envelope.model_dump()
    data["resource_scope"]["account_id"] = "another-account"
    with pytest.raises(ValidationError, match="payload_sha256"):
        ActionEnvelope.model_validate(data)


def test_approval_link_does_not_create_a_circular_digest(envelope):
    linked = ActionEnvelope.model_validate(envelope.model_dump() | {"approval_id": "approval1"})
    assert linked.payload_sha256 == envelope.payload_sha256
    assert approval_for(linked).matches(linked, NOW + timedelta(seconds=1))
    assert not approval_for(linked).matches(envelope, NOW + timedelta(seconds=1))


def test_nested_action_mutation_is_detected_before_approval_use(envelope):
    linked = ActionEnvelope.model_validate(envelope.model_dump() | {"approval_id": "approval1"})
    approval = approval_for(linked)
    linked.action.text = "modified after validation"
    assert not linked.verify_payload_hash()
    assert not approval.matches(linked, NOW + timedelta(seconds=1))
    with pytest.raises(ValidationError):
        ActionEnvelope.model_validate(linked.model_dump())


def test_nonfinite_nested_mutation_cannot_break_hash_check(envelope):
    envelope.action.x = float("nan")
    assert envelope.verify_payload_hash() is False


def test_existing_mutated_action_is_revalidated(envelope_fields):
    action = Action(type="click", x=10, y=20)
    action.x = -100
    with pytest.raises(ValidationError):
        ActionEnvelope.create(**(envelope_fields | {"action": action}))


def test_create_does_not_bypass_missing_invalid_or_unknown_fields(envelope_fields):
    for change in ({"fencing_token": 0}, {"task_revision": True}, {"tool_version": "   "},
                   {"schema_version": "2.0"}, {"action": {"type": "shell", "text": "x"}}):
        with pytest.raises((ValidationError, ValueError)):
            ActionEnvelope.create(**(envelope_fields | change))
    with pytest.raises(ValidationError):
        ActionEnvelope.create(**{key: value for key, value in envelope_fields.items() if key != "task_id"})
    with pytest.raises(ValueError):
        ActionEnvelope.create(**envelope_fields, undocumented=True)
    with pytest.raises(ValueError):
        ActionEnvelope.create(**envelope_fields, payload_sha256=DIGEST)


@pytest.mark.parametrize("change", [{"revoked": True}, {"task_revision": 2},
    {"action_id": "other_action"}, {"task_id": "other_task"}, {"payload_sha256": "b" * 64}])
def test_approval_is_exact_scoped_and_revocable(envelope, change):
    linked = ActionEnvelope.model_validate(envelope.model_dump() | {"approval_id": "approval1"})
    assert not approval_for(linked, **change).matches(linked, NOW + timedelta(seconds=1))


def test_approval_expiry_boundaries_and_naive_clock_rejected(envelope):
    linked = ActionEnvelope.model_validate(envelope.model_dump() | {"approval_id": "approval1"})
    approval = approval_for(linked)
    assert approval.matches(linked, NOW)
    assert not approval.matches(linked, NOW - timedelta(microseconds=1))
    assert not approval.matches(linked, approval.expires_at)
    with pytest.raises(ValueError):
        approval.matches(linked, NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        approval_for(linked, expires_at=NOW)


def test_lease_has_explicit_scope_and_nonzero_fence(scope):
    fields = dict(id="lease1", resource_id=scope.resource_id, resource_scope=scope, owner="task1",
        fencing_token=2, issued_at=NOW, expires_at=NOW + timedelta(minutes=5))
    assert ResourceLease(**fields).fencing_token == 2
    for change in ({"resource_id": "different"}, {"fencing_token": 0}, {"fencing_token": True},
                   {"expires_at": NOW}, {"revoked": 1}):
        with pytest.raises(ValidationError):
            ResourceLease(**(fields | change))


@pytest.mark.parametrize("fields", [
    {"resource_id": "*", "kind": "browser", "session_id": "session1"},
    {"resource_id": "browser1", "kind": "browser"},
    {"resource_id": "window1", "kind": "desktop", "session_id": "s", "device_id": "d", "pid": 10},
    {"resource_id": "file1", "kind": "filesystem", "path": "relative/file"},
    {"resource_id": "file1", "kind": "filesystem", "path": "/safe/../outside"},
    {"resource_id": "file1", "kind": "filesystem", "path": "C:\\safe\\..\\outside"},
    {"resource_id": "file1", "kind": "filesystem", "path": "/safe/*"},
    {"resource_id": "model1", "kind": "model", "website_origin": "http://user:secret@localhost"},
    {"resource_id": "model1", "kind": "model", "website_origin": "https://*.example.com"},
    {"resource_id": "model1", "kind": "model", "website_origin": "https://example.com/path"},
    {"resource_id": "model1", "kind": "model", "website_origin": "https://example.com:invalid"},
])
def test_ambiguous_resource_scope_rejected(fields):
    with pytest.raises(ValidationError):
        ResourceScope(**fields)


def test_explicit_windows_and_posix_paths_require_no_filesystem_io():
    for path in ("C:\\workspace\\report.txt", "/workspace/report.txt"):
        assert ResourceScope(resource_id="file1", kind="filesystem", path=path).path == path


def test_observation_requires_matching_expiry_and_real_revision(scope):
    fields = dict(id="obs1", task_id="task1", task_revision=3, revision=4, observed_at=NOW,
        expires_at=NOW + timedelta(seconds=30), resource_scope=scope, frame_version=4,
        coordinate_space="screenshot_pixels")
    assert ObservationRef(**fields).revision == 4
    for change in ({"revision": True}, {"observed_at": NOW.replace(tzinfo=None)},
                   {"expires_at": NOW}, {"image_digest": DIGEST}):
        with pytest.raises(ValidationError):
            ObservationRef(**(fields | change))


def test_planner_done_or_partial_stale_verdict_cannot_form_succeeded_task(task):
    for completion in (None, verdict_for(task, criterion_ids=["wrong"]),
                       verdict_for(task, task_revision=2), verdict_for(task, verdict="inconclusive")):
        with pytest.raises(ValidationError):
            Task.model_validate(task.model_dump() | {"status": "SUCCEEDED", "completion": completion})
    with pytest.raises(ValidationError):
        verdict_for(task, actor="planner")
    with pytest.raises(ValidationError):
        verdict_for(task, evidence_ids=[])
    completed = Task.model_validate(task.model_dump() | {"status": "SUCCEEDED", "completion": verdict_for(task)})
    assert completed.completion.actor == "external_verifier"
    assert completed.completion.evidence_ids == ("evidence1",)


def test_goal_and_criteria_cannot_be_silently_empty_or_duplicate(task):
    for change in ({"goal": "  "}, {"success_criteria": []}, {"status": "Completed"},
                   {"success_criteria": [task.success_criteria[0], task.success_criteria[0]]}):
        with pytest.raises(ValidationError):
            Task.model_validate(task.model_dump() | change)


def test_step_accepted_is_separate_from_verified():
    fields = dict(id="step1", task_id="task1", task_revision=3, plan_version=1,
        expected_effect="Profile persisted", verifier_ref="verifier1")
    assert Step(**fields, state="accepted").evidence_ids == ()
    for change in ({"state": "verified"}, {"dependencies": ["step1"]}, {"dependencies": ["x", "x"]}):
        with pytest.raises(ValidationError):
            Step(**(fields | change))
    assert Step(**fields, state="verified", evidence_ids=["evidence1"]).state == "verified"


def test_evidence_can_record_negative_and_uncertain_results_without_claiming_success():
    fields = dict(id="evidence1", task_id="task1", task_revision=3, observation_id="obs1",
        observation_revision=4, source="external_verifier", observed_at=NOW,
        check_type="browser_oracle", result="fail", summary="Saved value differs from requested value",
        verifier_ref="verifier1", criterion_ids=["saved"])
    evidence = Evidence(**fields)
    assert evidence.result == "fail"
    assert evidence.retention.mode == "summary_only"
    assert Evidence(**(fields | {"source": "planner", "result": "inconclusive", "verifier_ref": None})).source == "planner"
    with pytest.raises(ValidationError):
        Evidence(**(fields | {"verifier_ref": None}))
    with pytest.raises(ValidationError):
        Evidence(**(fields | {"result": "success"}))


def test_checkpoint_keeps_unknown_effects_and_cannot_enable_blind_replay():
    pending = PendingEffect(action_id="action1", payload_sha256=DIGEST,
                            state="outcome_unknown", idempotency_key="operation1")
    checkpoint = Checkpoint(id="checkpoint1", task_id="task1", task_revision=3, plan_version=1,
        pending_effects=[pending], verified_evidence_ids=["evidence1"], budget_remaining=Budget(max_steps=0), policy_digest=DIGEST)
    assert Checkpoint.model_validate_json(checkpoint.model_dump_json()) == checkpoint
    assert checkpoint.pending_effects[0].state == "outcome_unknown"
    for change in ({"reobserve_required": False}, {"reconcile_required": False},
                   {"reobserve_required": 1}, {"replay_pending_actions": "always"}):
        with pytest.raises(ValidationError):
            ResumeRules(**change)


def test_events_reject_non_json_payload_and_preserve_version_sequence():
    fields = dict(id="event1", sequence=10, timestamp=NOW, task_id="task1", task_revision=3,
        type="action.outcome_unknown", redacted_payload={"action_id": "action1", "result": "unknown"})
    event = Event(**fields)
    assert Event.model_validate_json(event.model_dump_json()) == event
    for change in ({"sequence": -1}, {"type": "arbitrary prose event"},
                   {"redacted_payload": {"value": float("nan")}},
                   {"redacted_payload": {"text": "a" * 33000}}):
        with pytest.raises(ValidationError):
            Event(**(fields | change))


def test_tool_manifest_never_adds_another_action_definition_or_fakes_integrity(scope):
    fields = dict(tool_id="computer_action", version="1.0.0", action_types=["click", "type"],
        input_schema=Action.model_json_schema(), resource_scopes=[scope], permissions=["gui.click", "gui.type"],
        side_effects="reversible", sandbox_profile="headless-browser",
        provenance={"source": "local-project"}, integrity={"expected_sha256": DIGEST})
    manifest = ToolManifest(**fields)
    assert manifest.integrity.verified is False
    assert ToolManifest.model_validate_json(manifest.model_dump_json()) == manifest
    for change in ({"action_types": ["shell"]}, {"input_schema": {"type": "unrecognized"}},
                   {"input_schema": {"$ref": "https://example.com/untrusted-schema"}},
                   {"integrity": {"expected_sha256": DIGEST, "verified_at": NOW}},
                   {"integrity": {"expected_sha256": DIGEST, "verified": True}}):
        with pytest.raises(ValidationError):
            ToolManifest(**(fields | change))


@pytest.mark.parametrize("change", [{"schema_version": "2.0"}, {"schema_version": 1}, {"unknown": "field"}, {"revision": "3"}])
def test_unknown_fields_versions_and_coerced_revisions_are_rejected(task, change):
    with pytest.raises(ValidationError):
        Task.model_validate(task.model_dump() | change)


def test_nested_unknown_versions_are_also_rejected(task):
    data = task.model_dump()
    data["success_criteria"][0]["schema_version"] = "2.0"
    with pytest.raises(ValidationError):
        Task.model_validate(data)


def test_validation_error_string_does_not_echo_private_payload(envelope):
    data = envelope.model_dump()
    data["action"]["url"] = "private-value-should-not-be-in-error"
    with pytest.raises(ValidationError) as error:
        ActionEnvelope.model_validate(data)
    assert "private-value-should-not-be-in-error" not in str(error.value)
