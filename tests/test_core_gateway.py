"""Candidate gateway integration with disposable real headless Chromium.

No model, physical input, production Run or existing service is used. Fixtures
own their SQLite stores and browser contexts; passing is not a release Gate.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest
import pytest_asyncio
from pydantic import ValidationError

from server.core.contracts import ActionEnvelope, Approval, ResourceScope, SuccessCriterion, Task
from server.core.gateway import ActionGateway, browser_manifest
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import AdmissionDenied, Conflict, OutcomeUnknown, TaskStore
from server.drivers import BrowserDriver
from server.grounding import StaleObservation
from server.schemas import Action


HTML = """<!doctype html><title>Gateway disposable fixture</title>
<style>body{margin:0;background:white}button{position:absolute;left:940px;top:500px;width:160px;height:60px}</style>
<button id="save">儲存測試</button><script>
window.saved=0;window.events=[];
for(const kind of ['pointermove','pointerdown','pointerup','click','keydown','keyup','input']) {
  document.addEventListener(kind,e=>{
    const event={kind,x:e.clientX??null,y:e.clientY??null,buttons:e.buttons??null,target:e.target.id};
    events.push(event); if(window.reportInput)window.reportInput(event);
  },true);
}
save.addEventListener('click',()=>saved++);
</script>"""
KINDS = ("click", "double_click", "move", "drag", "type", "key", "scroll")


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@dataclass
class World:
    path: Path
    clock: Clock
    store: TaskStore
    gateway: ActionGateway
    driver: BrowserDriver
    task: Task
    scope: ResourceScope
    lease: object
    frame: object
    inputs: list = field(default_factory=list)
    tasks: list = field(default_factory=list)

    def envelope(self, *, action=None, action_id="action-save", key="idem-save", approval_id=None):
        target = next(item["id"] for item in self.frame.snapshot["elements"] if item["text"] == "儲存測試")
        return ActionEnvelope.create(action_id=action_id, task_id=self.task.id,
            task_revision=self.task.revision, step_id="step-save", tool_id="browser.input",
            tool_version="1.0", action=action or Action(type="click", target=target),
            observation_id=self.frame.reference.id, observation_revision=self.frame.reference.revision,
            policy_ref=self.task.policy_ref, resource_scope=self.scope, approval_id=approval_id,
            lease_id=self.lease.id, fencing_token=self.lease.fencing_token,
            idempotency_key=key, created_at=self.clock())

    def approve(self, envelope):
        approval = Approval(id=envelope.approval_id, task_id=self.task.id,
            task_revision=self.task.revision, action_id=envelope.action_id,
            payload_sha256=envelope.payload_sha256, resource_scope=self.scope,
            principal="fixture-user", destination="isolated gateway fixture",
            issued_at=self.clock(), expires_at=self.clock() + timedelta(seconds=60))
        self.store.approve(approval)

    async def observed(self):
        return await self.driver._page.evaluate("({saved,events})")

    def start(self, envelope):
        task = asyncio.create_task(self.gateway.dispatch(envelope))
        self.tasks.append(task)
        return task

    def reopen(self):
        self.store.close()
        self.store = TaskStore(self.path, clock=self.clock)
        self.gateway.store = self.store


@pytest_asyncio.fixture
async def world(tmp_path):
    worlds = []

    async def create(*, approvals=()):
        index = len(worlds)
        path = tmp_path / f"candidate-{index}.sqlite3"
        clock = Clock()
        store = TaskStore(path, clock=clock)
        task = store.create_task(Task(id=f"task-{index}", goal="Click once in the isolated fixture",
            policy_ref="fixture-policy", created_at=clock(), updated_at=clock(),
            success_criteria=(SuccessCriterion(id="saved-once", description="Fixture counter equals one"),)))
        for status in ("QUEUED", "PLANNING", "READY", "RUNNING"):
            task = store.transition(task.id, task.revision, status)
        scope = ResourceScope(resource_id=f"browser-{index}", kind="browser", session_id=f"session-{index}")
        policies = PolicyRegistry()
        policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
            allowed_action_types=KINDS, approval_action_types=approvals))
        gateway = ActionGateway(store, policies)
        driver = BrowserDriver(headless=True)
        instance = World(path, clock, store, gateway, driver, task, scope, None, None)
        worlds.append(instance)
        await driver.start()
        await driver._page.set_content(HTML)
        await driver._page.expose_function("reportInput", lambda event: instance.inputs.append(event))
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, KINDS))
        instance.lease = store.acquire_lease(task.id, scope, ttl_seconds=120)
        instance.frame = await gateway.observe(task.id, task.revision, instance.lease)
        return instance

    yield create
    for instance in worlds:
        for task in instance.tasks:
            if not task.done():
                task.cancel()
        if instance.tasks:
            await asyncio.wait_for(asyncio.gather(*instance.tasks, return_exceptions=True), 5)
        try:
            current = instance.store.get_task(instance.task.id)
            await asyncio.wait_for(instance.gateway.stop(current.id, current.revision), 5)
        finally:
            await instance.driver.close()
            instance.store.close()


async def restart_parent(instance, *, approvals=(), legacy_v2=False):
    """Recreate the parent registry/store and its disposable browser, no model."""
    await instance.driver.close()
    instance.store.close()
    if legacy_v2:
        # Deliberately reconstruct the tracked v2 schema: it had the action
        # journal but no persistent policy table. This is fixture corruption,
        # never a runtime migration bypass or modification of a user database.
        with sqlite3.connect(instance.path) as db:
            db.execute("DROP TABLE policies")
            db.execute("PRAGMA user_version=2")
    instance.store = TaskStore(instance.path, clock=instance.clock)
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=instance.task.policy_ref,
        resource_scopes=(instance.scope,), allowed_action_types=KINDS,
        approval_action_types=approvals))
    instance.gateway = ActionGateway(instance.store, policies)
    instance.driver = BrowserDriver(headless=True)
    instance.inputs.clear()
    await instance.driver.start()
    await instance.driver._page.set_content(HTML)
    await instance.driver._page.expose_function("reportInput", lambda event: instance.inputs.append(event))
    return policies


async def test_real_click_is_journaled_once_and_replay_returns_cached_receipt(world):
    instance = await world()
    envelope = instance.envelope()
    first = await instance.gateway.dispatch(envelope)
    assert first["dispatch"] is True and first["state"] == "succeeded"
    assert first["outcome"]["task_completion_verified"] is False
    before = await instance.observed()
    assert before["saved"] == 1
    assert sum(event["kind"] == "click" for event in before["events"]) == 1
    second = await instance.gateway.dispatch(envelope)
    assert second == {**first, "dispatch": False}
    assert await instance.observed() == before
    assert sum(event.type == "action.dispatched" for event in instance.store.events(instance.task.id)) == 1
    assert instance.store.get_task(instance.task.id).status == "RUNNING"


async def test_concurrent_duplicate_dispatch_does_not_click_twice(world):
    instance = await world()
    envelope = instance.envelope()
    results = await asyncio.gather(instance.start(envelope), instance.start(envelope))
    assert sorted(item["dispatch"] for item in results) == [False, True]
    assert (await instance.observed())["saved"] == 1


async def test_reopened_unknown_dispatch_cannot_resend_same_or_new_action_identity(world):
    instance = await world()
    envelope = instance.envelope()
    # Simulate a crash after the durable pre-dispatch commit, before a receipt.
    # Reopening SQLite does not claim to recreate an OS/browser process.
    instance.store.admit_action(envelope, require_approval=False)
    instance.reopen()
    with pytest.raises(OutcomeUnknown):
        await instance.gateway.dispatch(envelope)
    with pytest.raises(OutcomeUnknown):
        await instance.gateway.dispatch(instance.envelope(action_id="different-id", key="different-key"))
    assert (await instance.observed()) == {"saved": 0, "events": []}
    assert len(instance.store.pending_effects(instance.task.id)) == 1


@pytest.mark.parametrize("change", ["label", "position", "disabled", "removed"])
async def test_semantically_stale_target_refused_without_any_input(world, change):
    instance = await world()
    envelope = instance.envelope()
    expressions = {"label": "save.textContent='Different destination'",
        "position": "save.style.left='600px'", "disabled": "save.disabled=true", "removed": "save.remove()"}
    await instance.driver._page.evaluate(expressions[change])
    with pytest.raises(StaleObservation):
        await instance.gateway.dispatch(envelope)
    assert (await instance.observed()) == {"saved": 0, "events": []}
    assert not any(event.type == "action.dispatched" for event in instance.store.events(instance.task.id))


async def test_missing_exact_approval_refused_before_any_input_or_journal(world):
    instance = await world(approvals=("click",))
    with pytest.raises(AdmissionDenied, match="approval"):
        await instance.gateway.dispatch(instance.envelope())
    assert (await instance.observed()) == {"saved": 0, "events": []}
    assert not any(event.type == "action.dispatched" for event in instance.store.events(instance.task.id))


async def test_superseded_observation_refused_before_input(world):
    instance = await world()
    old = instance.envelope()
    fresh = await instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease)
    assert fresh.reference.revision == instance.frame.reference.revision + 1
    with pytest.raises(AdmissionDenied, match="fresh observation"):
        await instance.gateway.dispatch(old)
    assert (await instance.observed()) == {"saved": 0, "events": []}


@pytest.mark.parametrize("field,value", [("button", "right"), ("text", "modified"),
    ("expected", "modified expectation"), ("memory", "modified memory")])
async def test_mutating_full_nested_action_invalidates_hash_before_input(world, field, value):
    instance = await world()
    envelope = instance.envelope()
    setattr(envelope.action, field, value)
    with pytest.raises(ValidationError, match="payload_sha256"):
        await instance.gateway.dispatch(envelope)
    assert (await instance.observed()) == {"saved": 0, "events": []}


async def test_same_driver_cannot_register_under_a_different_resource_alias(world):
    instance = await world()
    alias = ResourceScope(resource_id="alias-browser", kind="browser", session_id="alias-session")
    with pytest.raises(Conflict, match="canonical"):
        instance.gateway.register_browser(instance.task.id, alias, instance.driver, browser_manifest(alias, KINDS))
    assert (await instance.observed()) == {"saved": 0, "events": []}


async def test_headed_driver_registration_is_refused_without_starting_it(world):
    instance = await world()
    headed = BrowserDriver(headless=False)  # Never start this object.
    scope = ResourceScope(resource_id="headed-browser", kind="browser", session_id="headed-session")
    with pytest.raises(AdmissionDenied, match="headless"):
        instance.gateway.register_browser(instance.task.id, scope, headed, browser_manifest(scope, KINDS))
    assert headed._context is None and headed._browser is None


async def test_registered_driver_refuses_input_outside_an_active_gateway_dispatch(world):
    instance = await world()
    with pytest.raises(AdmissionDenied, match="active gateway"):
        await instance.driver.execute(instance.envelope().action)
    assert (await instance.observed()) == {"saved": 0, "events": []}


async def test_stop_mid_trajectory_revokes_database_before_interrupt_and_no_click(world, monkeypatch):
    instance = await world(approvals=("click",))
    envelope = instance.envelope(approval_id="approval-stop")
    instance.approve(envelope)
    at_interrupt = []
    before_close = []
    original_interrupt = instance.driver.interrupt_action
    original_close = instance.driver.close

    def inspect_interrupt():
        with sqlite3.connect(instance.path) as db:
            at_interrupt.append({"status": db.execute("SELECT status FROM tasks WHERE id=?", (instance.task.id,)).fetchone()[0],
                "lease_revoked": db.execute("SELECT revoked FROM leases WHERE id=?", (instance.lease.id,)).fetchone()[0],
                "approval_revoked": db.execute("SELECT revoked FROM approvals WHERE id=?", (envelope.approval_id,)).fetchone()[0]})
        original_interrupt()

    async def inspect_close():
        if instance.driver._page is not None and not instance.driver._page.is_closed():
            before_close.append(await instance.observed())
        await original_close()

    monkeypatch.setattr(instance.driver, "interrupt_action", inspect_interrupt)
    monkeypatch.setattr(instance.driver, "close", inspect_close)
    running = instance.start(envelope)
    await instance.driver._page.wait_for_function("events.filter(e=>e.kind==='pointermove').length>=5")
    stopped = await asyncio.wait_for(instance.gateway.stop(instance.task.id, instance.task.revision), 3)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert stopped.status == "CANCELLED"
    assert at_interrupt[0] == {"status": "CANCELLING", "lease_revoked": 1, "approval_revoked": 1}
    assert before_close[0]["saved"] == 0
    assert not any(event["kind"] in {"pointerdown", "click"} for event in before_close[0]["events"])
    assert instance.store.pending_effects(instance.task.id)[0]["state"] == "outcome_unknown"
    assert instance.driver._context is None and instance.driver._actions == set()
    before = len(instance.inputs)
    with pytest.raises(AdmissionDenied):
        await instance.gateway.dispatch(envelope)
    assert len(instance.inputs) == before


async def test_stop_mid_drag_drains_real_mouse_up_before_closing_browser(world, monkeypatch):
    instance = await world()
    envelope = instance.envelope(action=Action(type="drag", x=20, y=30, end_x=850, end_y=430, duration=1.8))
    closed_state = []
    original_close = instance.driver.close

    async def inspect_close():
        if instance.driver._page is not None and not instance.driver._page.is_closed():
            closed_state.append(await instance.observed())
        await original_close()

    monkeypatch.setattr(instance.driver, "close", inspect_close)
    running = instance.start(envelope)
    await instance.driver._page.wait_for_function("events.filter(e=>e.kind==='pointermove'&&e.buttons===1).length>=4")
    await asyncio.wait_for(instance.gateway.stop(instance.task.id, instance.task.revision), 3)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert sum(e["kind"] == "pointerdown" for e in closed_state[0]["events"]) == 1
    assert sum(e["kind"] == "pointerup" for e in closed_state[0]["events"]) == 1
    assert closed_state[0]["saved"] == 0
    assert instance.driver._context is None


async def test_other_coroutine_cannot_borrow_active_dispatch_for_unapproved_keyboard_input(world):
    instance = await world()
    drag = instance.envelope(action=Action(type="drag", x=20, y=30, end_x=850, end_y=430, duration=1.8))
    running = instance.start(drag)
    await instance.driver._page.wait_for_function("events.filter(e=>e.kind==='pointermove'&&e.buttons===1).length>=4")
    try:
        # This coroutine owns no ActionEnvelope. An unrelated in-flight drag
        # must not lend its authority to these direct driver keyboard commands.
        with pytest.raises(AdmissionDenied):
            await instance.driver.execute(Action(type="key", key="A"))
        assert not any(event["kind"] in {"keydown", "keyup", "input"}
                       for event in (await instance.observed())["events"])
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_stop_releases_owned_browser_even_if_task_is_already_failed(world):
    instance = await world()
    failed = instance.store.transition(instance.task.id, instance.task.revision, "FAILED")
    result = await instance.gateway.stop(failed.id, failed.revision)
    assert result.status == "FAILED"
    assert instance.driver._context is None and instance.driver._browser is None


async def test_restart_cannot_replace_pinned_policy_for_existing_or_fresh_task(world):
    instance = await world(approvals=("click",))
    original_digest = instance.gateway.policies.digest_for(instance.task.policy_ref)
    policies = await restart_parent(instance, approvals=())
    assert policies.digest_for(instance.task.policy_ref) != original_digest
    fresh = instance.store.create_task(Task(id="fresh-task", goal="Same immutable policy ID",
        policy_ref=instance.task.policy_ref, created_at=instance.clock(), updated_at=instance.clock(),
        success_criteria=(SuccessCriterion(id="same-policy", description="Must preserve approval"),)))
    for task_id in (instance.task.id, fresh.id):
        with pytest.raises(Conflict, match="policy ID"):
            instance.gateway.register_browser(task_id, instance.scope, instance.driver,
                browser_manifest(instance.scope, KINDS))
    instance.store.assert_policy(instance.task.policy_ref, original_digest)
    assert not instance.gateway._browsers
    assert (await instance.observed()) == {"saved": 0, "events": []}


async def test_same_policy_survives_restart_but_requires_fresh_observation_and_approval(world):
    instance = await world(approvals=("click",))
    old = instance.envelope()
    original_digest = instance.gateway.policies.digest_for(instance.task.policy_ref)
    policies = await restart_parent(instance, approvals=("click",))
    assert policies.digest_for(instance.task.policy_ref) == original_digest
    instance.gateway.register_browser(instance.task.id, instance.scope, instance.driver,
        browser_manifest(instance.scope, KINDS))
    with pytest.raises(AdmissionDenied, match="fresh observation"):
        await instance.gateway.dispatch(old)
    instance.store.release_lease(instance.lease.id, instance.lease.fencing_token)
    instance.lease = instance.store.acquire_lease(instance.task.id, instance.scope, ttl_seconds=120)
    instance.frame = await instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease)
    assert instance.frame.reference.id != old.observation_id
    with pytest.raises(AdmissionDenied, match="approval"):
        await instance.gateway.dispatch(instance.envelope())
    assert (await instance.observed()) == {"saved": 0, "events": []}
    envelope = instance.envelope(approval_id="approval-after-restart")
    instance.approve(envelope)
    receipt = await instance.gateway.dispatch(envelope)
    assert receipt["state"] == "succeeded"
    assert (await instance.observed())["saved"] == 1


@pytest.mark.parametrize("has_dispatch", [True, False], ids=["old-journal-refused", "empty-journal-can-pin"])
async def test_v2_upgrade_requires_explicit_policy_migration_only_for_existing_dispatches(world, has_dispatch):
    instance = await world()
    if has_dispatch:
        # Durable dispatch intent exists; no browser action has been sent.
        instance.store.admit_action(instance.envelope(), require_approval=False)
    original_task = instance.store.get_task(instance.task.id)
    original_events = instance.store.events(instance.task.id)
    policies = await restart_parent(instance, legacy_v2=True)
    assert instance.store.get_task(instance.task.id) == original_task
    assert instance.store.events(instance.task.id) == original_events
    with sqlite3.connect(instance.path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4
    if has_dispatch:
        with pytest.raises(AdmissionDenied, match="explicit migration"):
            instance.gateway.register_browser(instance.task.id, instance.scope, instance.driver,
                browser_manifest(instance.scope, KINDS))
        assert len(instance.store.pending_effects(instance.task.id)) == 1
        assert not instance.gateway._browsers
    else:
        instance.gateway.register_browser(instance.task.id, instance.scope, instance.driver,
            browser_manifest(instance.scope, KINDS))
        instance.store.assert_policy(instance.task.policy_ref, policies.digest_for(instance.task.policy_ref))
        assert not instance.store.pending_effects(instance.task.id)
    assert (await instance.observed()) == {"saved": 0, "events": []}
