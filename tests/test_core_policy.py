"""Parent policy checks use data only; no model, browser, desktop or OS sandbox."""
from datetime import datetime, timezone

from pydantic import ValidationError
import pytest

from server.schemas import Action
from server.core.contracts import ActionEnvelope, ResourceScope, ToolManifest
from server.core.policy import ExecutionPolicy, PolicyRegistry, origin_identity
from server.core.store import AdmissionDenied, Conflict

NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)
DIGEST = "a" * 64


@pytest.fixture
def scope():
    return ResourceScope(resource_id="browser1", kind="browser", session_id="session1",
                         website_origin="https://example.test")


def policy_for(scope, **updates):
    return ExecutionPolicy(**(dict(policy_id="policy1", resource_scopes=(scope,),
        allowed_action_types=("click", "type", "navigate", "new_tab", "wait")) | updates))


def action_for(scope, **updates):
    return ActionEnvelope.create(**(dict(action_id="action1", task_id="task1", task_revision=3,
        step_id="step1", tool_id="browser_action", tool_version="1.0.0",
        action=Action(type="click", target="e1"), observation_id="obs1", observation_revision=5,
        policy_ref="policy1", resource_scope=scope, lease_id="lease1", fencing_token=1,
        idempotency_key="operation1") | updates))


def manifest_for(scope, **updates):
    return ToolManifest(**(dict(tool_id="browser_action", version="1.0.0",
        action_types=("click", "type", "navigate", "new_tab", "wait"),
        input_schema=Action.model_json_schema(), resource_scopes=(scope,),
        permissions=("browser.input",), side_effects="reversible", sandbox_profile="headless-browser",
        provenance={"source": "parent-registered-executor"}, integrity={"expected_sha256": DIGEST,
            "observed_sha256": DIGEST, "verified_at": NOW, "verifier_ref": "parent-integrity-check"}) | updates))


def registered(scope, **policy_updates):
    registry = PolicyRegistry()
    registry.register(policy_for(scope, **policy_updates))
    return registry


def test_default_requires_approval_for_allowed_inputs_but_explicit_empty_is_auto(scope):
    policy = policy_for(scope)
    assert set(policy.approval_action_types) == {"click", "type", "navigate", "new_tab"}
    registry = registered(scope)
    result = registry.evaluate(action_for(scope), manifest_for(scope))
    assert result.require_approval is True and result.policy_digest == policy.digest
    wait = registry.evaluate(action_for(scope, action=Action(type="wait")), manifest_for(scope))
    assert wait.require_approval is False
    auto = registered(scope, approval_action_types=())
    assert auto.evaluate(action_for(scope), manifest_for(scope)).require_approval is False
    assert auto.evaluate(action_for(scope), manifest_for(scope)).policy_digest != policy.digest


def test_policy_id_is_immutable_and_equivalent_set_order_is_canonical(scope):
    registry = registered(scope)
    policy = policy_for(scope, allowed_action_types=("wait", "new_tab", "navigate", "type", "click"))
    registry.register(policy)
    assert policy.digest == policy_for(scope).digest
    for changed in (policy_for(scope, approval_action_types=()),
                    policy_for(scope, allowed_action_types=("click",)),
                    policy_for(scope.model_copy(update={"account_id": "another-account"}))):
        with pytest.raises(Conflict):
            registry.register(changed)


@pytest.mark.parametrize("updates", [
    {"allowed_action_types": ("shell",)}, {"allowed_action_types": ("click", "click")},
    {"approval_action_types": ("scroll",)}, {"approval_action_types": ("click", "click")},
    {"policy_id": "*"}, {"schema_version": "2.0"}, {"model_can_rewrite_policy": True},
])
def test_invalid_or_incompatible_policy_data_rejected(scope, updates):
    with pytest.raises(ValidationError):
        policy_for(scope, **updates)


def test_empty_allowed_actions_is_deny_all(scope):
    registry = registered(scope, allowed_action_types=())
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope), manifest_for(scope))


def test_registered_policy_is_a_snapshot_not_callers_mutable_object(scope):
    policy = policy_for(scope)
    registry = PolicyRegistry()
    registry.register(policy)
    # An unchecked model_copy or object mutation must not rewrite registry state.
    object.__setattr__(policy, "approval_action_types", ())
    assert registry.evaluate(action_for(scope), manifest_for(scope)).require_approval is True
    with pytest.raises(Conflict):
        registry.register(policy)


def test_digest_lookup_uses_immutable_snapshot_and_rejects_unknown_id(scope):
    registry = PolicyRegistry()
    policy = policy_for(scope)
    expected = policy.digest
    registry.register(policy)
    assert registry.digest_for(policy.policy_id) == expected
    object.__setattr__(policy, "approval_action_types", ())
    assert registry.digest_for(policy.policy_id) == expected
    with pytest.raises(AdmissionDenied):
        registry.digest_for("not-registered")


@pytest.mark.parametrize("updates", [
    {"session_id": "another-session"}, {"resource_id": "another-resource"},
    {"website_origin": "https://another.test"}, {"account_id": "another-account"},
])
def test_policy_requires_complete_exact_scope_not_just_resource_id(scope, updates):
    changed = ResourceScope.model_validate(scope.model_dump() | updates)
    with pytest.raises(AdmissionDenied):
        registered(scope).evaluate(action_for(changed), manifest_for(changed))


def test_unregistered_policy_and_unlisted_action_are_denied(scope):
    registry = registered(scope, allowed_action_types=("type",))
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope), manifest_for(scope))
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope, policy_ref="unknown-policy"), manifest_for(scope))


@pytest.mark.parametrize("change", [
    {"tool_id": "unregistered_tool"}, {"version": "2.0.0"}, {"action_types": ("type",)},
    {"resource_scopes": ()}, {"integrity": {"expected_sha256": DIGEST}},
    {"integrity": {"expected_sha256": DIGEST, "observed_sha256": "b" * 64,
                   "verified_at": NOW, "verifier_ref": "parent-integrity-check"}},
])
def test_manifest_tool_version_action_scope_and_integrity_must_match(scope, change):
    with pytest.raises(AdmissionDenied):
        registered(scope).evaluate(action_for(scope), manifest_for(scope, **change))


def test_schema_is_actually_applied_to_action_arguments(scope):
    schema = {"type": "object", "properties": {"type": {"const": "click"},
        "target": {"enum": ["e_allowed"]}}, "required": ["type", "target"], "additionalProperties": False}
    registry = registered(scope)
    manifest = manifest_for(scope, input_schema=schema)
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope), manifest)
    allowed = action_for(scope, action=Action(type="click", target="e_allowed"))
    assert registry.evaluate(allowed, manifest).require_approval


def test_manifest_cannot_make_side_effect_approval_optional_by_claiming_no_effects(scope):
    result = registered(scope).evaluate(action_for(scope), manifest_for(scope, side_effects="none"))
    assert result.require_approval is True


def test_models_cannot_insert_permissions_or_manifest_into_action_envelope(scope):
    data = action_for(scope).model_dump()
    for extra in ({"manifest": manifest_for(scope).model_dump()}, {"permissions": ["filesystem.write"]}):
        with pytest.raises(ValidationError):
            ActionEnvelope.model_validate(data | extra)


def test_nested_invalid_action_and_manifest_mutations_fail_closed(scope):
    registry = registered(scope)
    envelope = action_for(scope)
    envelope.action.target = "modified after approval hash"
    with pytest.raises(AdmissionDenied):
        registry.evaluate(envelope, manifest_for(scope))
    manifest = manifest_for(scope)
    manifest.input_schema["$ref"] = "https://outside.test/schema"
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope), manifest)


@pytest.mark.parametrize("url", ["https://example.test/path?q=1#fragment", "https://EXAMPLE.test:443/path"])
def test_same_origin_navigation_is_allowed(scope, url):
    registry = registered(scope)
    for kind in ("navigate", "new_tab"):
        result = registry.evaluate(action_for(scope, action=Action(type=kind, url=url)), manifest_for(scope))
        assert result.require_approval


@pytest.mark.parametrize("url", [
    "http://example.test/path", "https://example.test:444/path", "https://other.test/path",
    "https://example.test.evil.test/path", "https://example.test:0/path",
    "https://example.test%2f.evil.test/path", "https://example.test\\evil.test/path",
    "https://example.test\n/path",
])
def test_navigation_outside_exact_origin_or_ambiguous_parser_input_is_denied(scope, url):
    with pytest.raises(AdmissionDenied):
        registered(scope).evaluate(action_for(scope, action=Action(type="navigate", url=url)), manifest_for(scope))


def test_origin_restricted_new_tab_requires_destination_before_any_dispatch(scope):
    with pytest.raises(AdmissionDenied):
        registered(scope).evaluate(action_for(scope, action=Action(type="new_tab")), manifest_for(scope))


def test_blank_new_tab_remains_available_when_origin_is_not_restricted(scope):
    scope = ResourceScope.model_validate(scope.model_dump() | {"website_origin": None})
    result = registered(scope).evaluate(action_for(scope, action=Action(type="new_tab")), manifest_for(scope))
    assert result.require_approval


def test_shared_origin_identity_preserves_explicit_zero_port_and_normalizes_dns():
    assert origin_identity("https://EXAMPLE.test") == ("https", "example.test", 443)
    assert origin_identity("https://example.test:0") == ("https", "example.test", 0)
    assert origin_identity("http://[::1]:8080/path") == ("http", "::1", 8080)
    assert origin_identity("https://例子.test") == origin_identity("https://xn--fsqu00a.test")


def test_native_and_desktop_action_cannot_masquerade_as_browser(scope):
    native = ResourceScope(resource_id="window1", kind="desktop", device_id="device1",
                           session_id="native1", window_id=99, pid=123)
    with pytest.raises(AdmissionDenied):
        registered(native)
    with pytest.raises(AdmissionDenied):
        registered(scope).evaluate(action_for(native), manifest_for(native))
    # Even a misconfigured parent policy/manifest cannot enable open_app by
    # falsely labelling that native operation with a browser resource scope.
    registry = registered(scope, allowed_action_types=("open_app",))
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope, action=Action(type="open_app", app="Example")),
                          manifest_for(scope, action_types=("open_app",)))


@pytest.mark.parametrize("kind", ["done", "ask_user"])
def test_parent_completion_and_questions_are_not_driver_policy_actions(scope, kind):
    registry = registered(scope, allowed_action_types=(kind,))
    with pytest.raises(AdmissionDenied):
        registry.evaluate(action_for(scope, action=Action(type=kind, text="message")),
                          manifest_for(scope, action_types=(kind,)))
