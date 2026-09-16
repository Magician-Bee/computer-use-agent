"""Parent facade regressions: real SQLite, bounded fakes; no model inference."""
import asyncio
import copy
from datetime import timedelta
import json
from types import SimpleNamespace
import uuid

from jsonschema import Draft202012Validator
import pytest

from server.core.computer_tools import ComputerTools, perception_transform, planner_observation
from server.core.contracts import ObservationRef, ResourceScope, SuccessCriterion, Task
from server.core.gateway import ActionGateway, ObservedFrame, browser_manifest
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import AdmissionDenied, OutcomeUnknown, TaskStore
from server.core.verifiers import BrowserCondition, CheckResult, VerifierRegistry
from server.drivers import BrowserDriver
from server.schemas import ModelConfig


def snapshot():
    return {"target": "browser", "width": 1280, "height": 800, "title": "Disposable fixture",
        "url": "about:blank", "text": "Save", "_grounding_text": "Raw DOM Save",
        "image": "PRIVATE_IMAGE", "verifier": {"expected": "PRIVATE_ORACLE"},
        "physical_input_untouched": True, "elements": [
            {"id": "e1", "role": "button", "text": "Save", "x": 150, "y": 100,
             "width": 80, "height": 40, "source": "dom"}]}


class FakeGateway:
    def __init__(self, store):
        self.store, self.snapshot = store, snapshot()
        self.observe_error = None
        self.dispatch_mode = "success"
        self.dispatched = []

    async def observe(self, task_id, revision, lease, *, ttl_seconds, quick=False):
        if self.observe_error:
            raise self.observe_error
        self.store.validate_lease(task_id, revision, lease.resource_scope, lease.id, lease.fencing_token)
        now = self.store.clock()
        ref = ObservationRef(id=uuid.uuid4().hex, task_id=task_id, task_revision=revision,
            revision=self.store.latest_observation_revision(task_id, lease.resource_id) + 1,
            observed_at=now, expires_at=now + timedelta(seconds=ttl_seconds),
            resource_scope=lease.resource_scope, frame_version=1, coordinate_space="screenshot_pixels")
        self.store.record_observation(ref)
        return ObservedFrame(ref, copy.deepcopy(self.snapshot))

    async def dispatch(self, envelope):
        if self.dispatch_mode == "before_admission":
            raise AdmissionDenied("private preflight failure")
        self.store.admit_action(envelope, require_approval=False)
        self.dispatched.append(envelope)
        if self.dispatch_mode == "unknown":
            self.store.mark_outcome_unknown(envelope.action_id)
            raise AdmissionDenied("private interrupted side effect")
        self.store.record_outcome(envelope.action_id, {"executor_returned": True}, succeeded=True)
        if self.dispatch_mode == "after_result":
            raise AdmissionDenied("private response delivery failure")
        return {"state": "succeeded", "outcome": {"executor_returned": True}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "authority.sqlite3")
    task = store.create_task(Task(id="facade-task", goal="Save the specified fixture result", policy_ref="parent-policy",
        success_criteria=(SuccessCriterion(id="saved", description="Result was saved", verifier_ref="parent-verifier"),)))
    for status in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, status)
    store.pin_policy(task.policy_ref, "1" * 64)
    scope = ResourceScope(resource_id="owned-browser", kind="browser", session_id="fixture-session")
    registry = VerifierRegistry()
    result = SimpleNamespace(status="pass", error=None)

    async def verify(*_):
        if result.error:
            raise result.error
        return (CheckResult(criterion_id="saved", status=result.status,
                           reason="oracle_pass" if result.status == "pass" else "oracle_fail"),)

    registry.register_callback(task, verify, verifier_id="parent-verifier")
    monkeypatch.setattr(registry, "verify", verify)
    gateway = FakeGateway(store)
    facade = ComputerTools(store, gateway, registry, task_id=task.id, scope=scope, driver=BrowserDriver(),
                           allowed_action_types=("click", "type", "key", "wait", "select_option"))
    yield SimpleNamespace(store=store, task=task, scope=scope, registry=registry, result=result,
                          gateway=gateway, tools=facade)
    store.close()


def click(target="e1"):
    return {"action": {"type": "click", "target": target, "reason": "Activate the observed button"}}


def action_schema(world):
    return next(tool["parameters"] for tool in world.tools.definitions() if tool["name"] == "computer_act")


async def test_observation_is_bounded_json_without_image_or_oracle_and_is_copied(world):
    view = await world.tools("computer_observe", {})
    assert "PRIVATE" not in json.dumps(view)
    assert "_grounding_text" not in view
    assert len(json.dumps(view, ensure_ascii=False)) < 24000
    view["elements"][0]["id"] = "caller-mutation"
    assert world.tools.model_view["elements"][0]["id"] == "e1"
    assert Draft202012Validator(action_schema(world)).is_valid(click())
    assert not Draft202012Validator(action_schema(world)).is_valid(click("stale"))


def test_prepare_and_transport_caps_both_report_element_truncation():
    raw = snapshot()
    raw["elements"] = [{**raw["elements"][0], "id": f"e{i}"} for i in range(251)]
    result = planner_observation(raw)
    assert result["elements_truncated"] is True
    assert len(json.dumps(result, ensure_ascii=False)) <= 21000
    assert raw["elements"][-1]["id"] == "e250"
    raw["text"] = "x" * 30000
    result = planner_observation(raw)
    assert result["text_truncated"] is True and len(result["text"]) == 10000


def test_temporarily_unavailable_action_has_valid_unsatisfiable_schema(world):
    world.tools.allowed_action_types = ("select_option",)
    schema = action_schema(world)
    Draft202012Validator.check_schema(schema)
    assert not Draft202012Validator(schema).is_valid({"action": {"type": "select_option", "target": "e1", "text": "x"}})


@pytest.mark.parametrize("name,arguments", [
    ("computer_observe", {"policy_ref": "forged"}),
    ("computer_act", {**click(), "lease_id": "forged"}),
    ("computer_act", {"action": {**click()["action"], "approval_id": "forged"}}),
    ("computer_finish", {"summary": "done", "verifier_id": "forged"}),
    ("computer_finish", {"summary": "done", "evidence": {"result": "pass"}}),
    ("shell", {"command": "not-a-tool"}),
])
async def test_model_cannot_choose_authority_verifier_or_other_tool(world, name, arguments):
    await world.tools("computer_observe", {})
    before = world.store.get_task(world.task.id)
    result = await world.tools(name, arguments)
    assert result["executed"] is False
    assert world.store.get_task(world.task.id) == before and not world.gateway.dispatched


async def test_parent_constructs_exact_envelope_and_refreshes_frame_after_action(world):
    observed = await world.tools("computer_observe", {})
    result = await world.tools("computer_act", click())
    assert result["receipt"]["state"] == "succeeded" and result["verified"] is False
    envelope = world.gateway.dispatched[0]
    assert envelope.policy_ref == "parent-policy" and envelope.resource_scope == world.scope
    assert envelope.observation_id == observed["observation_id"]
    assert envelope.observation_revision == observed["observation_revision"]
    assert envelope.lease_id == world.tools.lease.id and envelope.verify_payload_hash()
    assert world.tools.frame.reference.id == result["observation"]["observation_id"]
    assert world.tools.frame.reference.revision > envelope.observation_revision
    await world.tools("computer_act", click())
    assert len(world.gateway.dispatched) == 2


@pytest.mark.parametrize("invalidate", ["task_revision", "new_observation"])
async def test_current_schema_drops_stale_frame_before_proposal(world, invalidate):
    await world.tools("computer_observe", {})
    if invalidate == "task_revision":
        task = world.store.get_task(world.task.id)
        task = world.store.transition(task.id, task.revision, "PAUSED")
        task = world.store.transition(task.id, task.revision, "REOBSERVE")
        world.store.transition(task.id, task.revision, "RUNNING")
    else:
        await world.gateway.observe(world.task.id, world.task.revision, world.tools.lease, ttl_seconds=120)
    assert not Draft202012Validator(action_schema(world)).is_valid(click())
    assert world.tools.frame is None
    result = await world.tools("computer_act", click())
    assert result["executed"] is False and not world.gateway.dispatched


async def test_revoked_cached_lease_is_replaced_for_new_observation(world):
    await world.tools("computer_observe", {})
    old = world.tools.lease
    world.store.release_lease(old.id, old.fencing_token)
    result = await world.tools("computer_observe", {})
    assert "observation_id" in result
    assert world.tools.lease.id != old.id and world.tools.lease.fencing_token > old.fencing_token


@pytest.mark.parametrize("mode", ["before_admission", "unknown", "after_result"])
async def test_exception_result_classification_uses_persistent_action_ledger(world, mode):
    await world.tools("computer_observe", {})
    world.gateway.dispatch_mode = mode
    result = await world.tools("computer_act", click())
    assert "private" not in json.dumps(result)
    if mode == "before_admission":
        assert result["executed"] is False and not world.gateway.dispatched
    elif mode == "unknown":
        assert result["error"] == "outcome_unknown" and "executed" not in result
        assert world.store.get_task(world.task.id).status == "RECONCILING"
        assert len(world.store.pending_effects(world.task.id)) == 1
    else:
        assert result["receipt"]["state"] == "succeeded" and "executed" not in result
        assert len(world.tools.actions) == 1


async def test_failed_verification_allows_correction_but_success_requires_recorded_evidence(world):
    world.result.status = "fail"
    failed = await world.tools("computer_finish", {"summary": "I declare success"})
    assert failed["verified"] is False and failed["task_status"] == "RUNNING"
    assert world.tools.frame.reference.task_revision == world.store.get_task(world.task.id).revision
    assert failed["observation"]["observation_id"] == world.tools.frame.reference.id
    assert world.store.get_task(world.task.id).completion is None
    world.result.status = "pass"
    passed = await world.tools("computer_finish", {"summary": "Please verify independently"})
    task = world.store.get_task(world.task.id)
    assert passed["verified"] is True and task.status == "SUCCEEDED"
    assert task.completion.actor == "external_verifier" and task.completion.evidence_ids
    assert task.completion.verifier_ref == "parent-verifier"
    assert any(event.type == "evidence.recorded" for event in world.store.events(task.id))


@pytest.mark.parametrize("where", ["observe", "registry", "record_evidence"])
async def test_verification_exception_recovers_same_revision_and_returns_no_exception_data(world, monkeypatch, where):
    error = RuntimeError("private verifier internals")
    if where == "observe":
        world.gateway.observe_error = error
    elif where == "registry":
        world.result.error = error
    else:
        monkeypatch.setattr(world.store, "record_evidence", lambda *_: (_ for _ in ()).throw(error))
    result = await world.tools("computer_finish", {"summary": "Check"})
    assert result["error"] == "verification_unavailable" and result["verified"] is False
    assert result["task_status"] == "RUNNING" and result["next"] == "computer_observe"
    assert world.store.get_task(world.task.id).status == "RUNNING"
    assert "private" not in json.dumps(result)


async def test_unresolved_effect_during_finish_moves_to_reconciling_not_retrying(world):
    await world.tools("computer_observe", {})
    world.gateway.dispatch_mode = "unknown"
    await world.tools("computer_act", click())
    # A parent may request another readback while reconciling; unresolved input
    # still forbids a successful verdict even if the current UI appears right.
    task = world.store.get_task(world.task.id)
    task = world.store.transition(task.id, task.revision, "REOBSERVE")
    world.store.transition(task.id, task.revision, "RUNNING")
    result = await world.tools("computer_finish", {"summary": "Looks done"})
    assert result["error"] == "outcome_unknown"
    assert world.store.get_task(world.task.id).status == "RECONCILING"


@pytest.mark.parametrize("stop", [False, True])
async def test_cancel_verification_propagates_and_never_revives_stopped_task(world, monkeypatch, stop):
    waiting = asyncio.Event()

    async def blocked(*_):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(world.registry, "verify", blocked)
    active = asyncio.create_task(world.tools("computer_finish", {"summary": "Check"}))
    await asyncio.wait_for(waiting.wait(), 1)
    current = world.store.get_task(world.task.id)
    assert current.status == "VERIFYING"
    if stop:
        world.store.request_stop(current.id, current.revision)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert world.store.get_task(current.id).status == ("CANCELLING" if stop else "REOBSERVE")


async def test_verifier_error_does_not_overwrite_concurrent_parent_pause(world, monkeypatch):
    async def interrupted(*_):
        current = world.store.get_task(world.task.id)
        world.store.transition(current.id, current.revision, "PAUSED")
        raise RuntimeError("private paused verifier")

    monkeypatch.setattr(world.registry, "verify", interrupted)
    result = await world.tools("computer_finish", {"summary": "Check"})
    assert result["task_status"] == "PAUSED" and result["input_allowed"] is False
    assert "next" not in result and world.store.get_task(world.task.id).status == "PAUSED"


async def test_ask_user_suspends_input_and_cannot_self_resume(world):
    await world.tools("computer_observe", {})
    result = await world.tools("computer_ask_user", {"question": "Which value should I use?"})
    assert result["task_status"] == "WAITING_USER" and world.tools.frame is None
    assert world.tools.pending_question == "Which value should I use?"
    blocked = await world.tools("computer_observe", {})
    assert blocked["task_status"] == "WAITING_USER"


async def test_cancel_after_admission_propagates_and_keeps_unknown_ledger(world, monkeypatch):
    waiting = asyncio.Event()

    async def interrupted(envelope):
        world.store.admit_action(envelope, require_approval=False)
        waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            world.store.mark_outcome_unknown(envelope.action_id)
            raise

    await world.tools("computer_observe", {})
    monkeypatch.setattr(world.gateway, "dispatch", interrupted)
    active = asyncio.create_task(world.tools("computer_act", click()))
    await asyncio.wait_for(waiting.wait(), 1)
    active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert world.tools.frame is None and len(world.store.pending_effects(world.task.id)) == 1


async def test_perception_transform_preserves_gateway_grounding_and_quick_skips_glm(monkeypatch):
    calls = []

    async def enrich(value, **kwargs):
        calls.append((copy.deepcopy(value), kwargs))
        value["text"] += "\nOCR transcript"
        value["elements"].append({"id": "ocr_1", "source": "apple_vision", "text": "Save"})
        return value

    monkeypatch.setattr("server.core.computer_tools.enrich_observation", enrich)
    transform = perception_transform(ModelConfig(ocr_engine="glm_ocr"), no_dom=True)
    raw = snapshot()
    full, quick = await transform(raw, quick=False), await transform(raw, quick=True)
    assert calls[0][1]["ocr_engine"] == "glm_ocr" and calls[1][1]["ocr_engine"] == "native"
    assert all(not call[0]["elements"] for call in calls)
    assert full["_grounding_text"] == quick["_grounding_text"] == raw["_grounding_text"]
    assert full["image"] == raw["image"] and raw["elements"][0]["source"] == "dom"


@pytest.mark.parametrize("no_dom", [False, True])
async def test_real_headless_facade_save_and_external_verdict(tmp_path, monkeypatch, no_dom):
    """Actual pointer input + SQLite + fixed DOM verifier, no model or real OCR."""
    enrichment_calls = []

    async def fake_ocr(raw, **kwargs):
        enrichment_calls.append(kwargs["ocr_engine"])
        assert raw["elements"] == []
        raw["elements"] = [{"id": "ocr_1", "source": "apple_vision", "role": "text", "text": "Save",
                            "x": 580, "y": 430, "width": 160, "height": 60, "confidence": 0.99}]
        raw["text"] += "\nSave"
        raw["perception"] = {"ocr_engine": "fake_native_ocr"}
        return raw

    monkeypatch.setattr("server.core.computer_tools.enrich_observation", fake_ocr)
    store = TaskStore(tmp_path / "real-authority.sqlite3")
    task = store.create_task(Task(id="real-facade", goal="Save this disposable test fixture", policy_ref="fixture-policy",
        success_criteria=(SuccessCriterion(id="saved", description="Fixture reports Saved"),)))
    for status in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, status)
    scope = ResourceScope(resource_id="real-browser", kind="browser", session_id="owned-headless-session")
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
                                     allowed_action_types=("click",), approval_action_types=()))
    transform = perception_transform(ModelConfig(ocr_engine="native"), no_dom=no_dom)
    gateway = ActionGateway(store, policies, observation_transform=transform)
    registry = VerifierRegistry()
    registry.register(task, (BrowserCondition(id="status-text", criterion_id="saved", kind="text",
                                             selector="#status", value="Saved"),))
    driver = BrowserDriver()
    try:
        await driver.start()
        await driver._page.set_content('''<!doctype html><title>Disposable facade test</title>
<button id="save" style="position:absolute;left:500px;top:400px;width:160px;height:60px">Save</button>
<p id="status"></p><script>window.saves=0;save.onclick=()=>{saves++;status.textContent='Saved';};</script>'''.replace("status.textContent", "document.getElementById('status').textContent"))
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("click",)))
        facade = ComputerTools(store, gateway, registry, task_id=task.id, scope=scope,
                               driver=driver, allowed_action_types=("click",))
        failed = await facade("computer_finish", {"summary": "Check current outcome"})
        assert failed["verified"] is False and store.get_task(task.id).status == "RUNNING"
        observed = await facade("computer_observe", {})
        assert "image" not in observed and "_grounding_text" not in observed
        source = "apple_vision" if no_dom else "dom"
        target = next(element["id"] for element in observed["elements"] if element["text"] == "Save")
        assert all(element["source"] == source for element in observed["elements"])
        result = await facade("computer_act", click(target))
        assert result["receipt"]["state"] == "succeeded" and result["verified"] is False
        assert facade.frame.reference.id == result["observation"]["observation_id"]
        passed = await facade("computer_finish", {"summary": "Please verify the saved result"})
        assert passed["verified"] is True
        assert await driver._page.evaluate("window.saves") == 1
        persisted = store.get_task(task.id)
        assert persisted.status == "SUCCEEDED" and persisted.completion.evidence_ids
        assert len(facade.actions) == 1
        assert bool(enrichment_calls) is no_dom
    finally:
        await driver.close()
        store.close()


@pytest.fixture
async def live_world(tmp_path):
    """Two distinct real DOM targets, with a controllable async readback hook."""
    store = TaskStore(tmp_path / "feedback.sqlite3")
    task = store.create_task(Task(id="feedback", goal="Advance then save the disposable fixture", policy_ref="feedback-policy",
        success_criteria=(SuccessCriterion(id="saved", description="Fixture state reports Saved"),)))
    for status in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, status)
    scope = ResourceScope(resource_id="feedback-browser", kind="browser", session_id="feedback-session")
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
        allowed_action_types=("click",), approval_action_types=()))
    state = SimpleNamespace(hook=None, quick_calls=[])

    async def transform(raw, *, quick):
        state.quick_calls.append(quick)
        if state.hook:
            await state.hook(quick)
        raw["perception"] = {"engine": "fixture_fake_no_model"}
        return raw

    gateway = ActionGateway(store, policies, observation_transform=transform)
    registry = VerifierRegistry()
    registry.register(task, (BrowserCondition(id="saved-text", criterion_id="saved", kind="text", selector="#status", value="Saved"),))
    driver = BrowserDriver(headless=True)
    await driver.start()
    try:
        await driver._page.set_content('''<!doctype html><title>Feedback fixture</title>
<button id="advance" style="position:absolute;left:350px;top:350px;width:150px;height:50px">Advance</button>
<p id="status">Pending</p><script>window.clicks=0;
advance.onclick=()=>{clicks++;const next=document.createElement('button');next.textContent='Save';next.id='save';
next.style='position:absolute;left:600px;top:350px;width:150px;height:50px';
next.onclick=()=>{clicks++;document.getElementById('status').textContent='Saved';};advance.replaceWith(next);};</script>''')
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("click",)))
        tools = ComputerTools(store, gateway, registry, task_id=task.id, scope=scope,
                              driver=driver, allowed_action_types=("click",))
        yield SimpleNamespace(store=store, task=task, scope=scope, gateway=gateway, driver=driver,
                              tools=tools, state=state)
    finally:
        current = store.get_task(task.id)
        await asyncio.wait_for(gateway.stop(task.id, current.revision), 5)
        await driver.close()
        store.close()


def visible_target(observation, text):
    return next(item["id"] for item in observation["elements"] if item["text"] == text)


async def test_two_real_actions_use_automatic_fresh_views_without_manual_observe(live_world):
    world = live_world
    first = await world.tools("computer_observe", {})
    old = visible_target(first, "Advance")
    changed = await world.tools("computer_act", click(old))
    second_view = changed["observation"]
    assert changed["receipt"]["state"] == "succeeded" and changed["input_allowed"]
    assert second_view["observation_revision"] > first["observation_revision"]
    new = visible_target(second_view, "Save")
    assert new != old
    assert not Draft202012Validator(action_schema(world)).is_valid(click(old))
    saved = await world.tools("computer_act", click(new))
    assert saved["receipt"]["state"] == "succeeded" and "Saved" in saved["observation"]["text"]
    assert await world.driver._page.evaluate("clicks") == 2
    assert [item["name"] for item in world.tools.calls] == ["computer_observe", "computer_act", "computer_act"]
    assert world.state.quick_calls == [False, True, False, True, False]
    assert len(world.tools.actions) == 2 and not world.store.pending_effects(world.task.id)


async def test_failed_finish_returns_new_running_revision_frame_and_uses_quick_evidence(live_world):
    world = live_world
    failed = await world.tools("computer_finish", {"summary": "I declare it finished"})
    current = world.store.get_task(world.task.id)
    assert not failed["verified"] and current.status == "RUNNING"
    assert world.state.quick_calls == [True, False]
    assert world.tools.frame.reference.task_revision == current.revision > world.task.revision
    assert failed["observation"]["observation_id"] == world.tools.frame.reference.id
    target = visible_target(failed["observation"], "Advance")
    assert Draft202012Validator(action_schema(world)).is_valid(click(target))
    result = await world.tools("computer_act", click(target))
    assert result["receipt"]["state"] == "succeeded"


async def test_post_action_readback_error_preserves_actual_receipt_and_forbids_blind_retry(live_world):
    world = live_world
    observed = await world.tools("computer_observe", {})
    target = visible_target(observed, "Advance")

    async def fail_full(quick):
        if not quick:
            raise RuntimeError("private OCR failure must not escape")

    world.state.hook = fail_full
    result = await world.tools("computer_act", click(target))
    assert result["receipt"]["state"] == "succeeded" and "executed" not in result
    assert result["observation_error"] == "fresh_observation_unavailable" and not result["input_allowed"]
    assert "private" not in json.dumps(result) and "observation" not in result
    assert world.tools.frame is None and world.tools.model_view is None
    assert await world.driver._page.evaluate("clicks") == 1
    envelope = world.tools.actions[0]
    assert world.store.existing_action_result(envelope)["state"] == "succeeded"
    rejected = await world.tools("computer_act", click(target))
    assert rejected["executed"] is False and len(world.tools.actions) == 1
    assert not world.store.pending_effects(world.task.id)


@pytest.mark.parametrize("stop", [False, True])
async def test_cancel_automatic_readback_keeps_known_action_and_propagates(live_world, stop):
    world = live_world
    observed = await world.tools("computer_observe", {})
    waiting = asyncio.Event()

    async def blocked_full(quick):
        if not quick:
            waiting.set()
            await asyncio.Event().wait()

    world.state.hook = blocked_full
    active = asyncio.create_task(world.tools("computer_act", click(visible_target(observed, "Advance"))))
    await asyncio.wait_for(waiting.wait(), 4)
    assert await world.driver._page.evaluate("clicks") == 1
    envelope = world.tools.actions[0]
    if stop:
        current = world.store.get_task(world.task.id)
        await asyncio.wait_for(world.gateway.stop(current.id, current.revision), 4)
    else:
        active.cancel()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert world.tools.frame is None and world.tools.model_view is None
    assert world.store.existing_action_result(envelope)["state"] == "succeeded"
    assert not world.store.pending_effects(world.task.id)
    assert world.store.get_task(world.task.id).status == ("CANCELLED" if stop else "RUNNING")
