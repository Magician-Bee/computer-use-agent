"""Controlled pause/resume seam: temporary SQLite and real headless Chromium.

No model, native input, user clipboard or existing app/service is exercised.
The pending-key test deliberately interposes one in-flight transport shim;
it proves real browser key release, not atomic cancellation inside Chromium.
"""
import asyncio
import sqlite3

import pytest

from server.core.store import AdmissionDenied, Conflict, OutcomeUnknown
from server.drivers import DriverAbort
from server.schemas import Action
from test_core_gateway import world  # Reuse the disposable browser/store fixture.


async def test_pause_mid_drag_revokes_first_releases_button_and_preserves_browser(world, monkeypatch):
    instance = await world(approvals=("drag",))
    page, context = instance.driver._page, instance.driver._context
    envelope = instance.envelope(action=Action(type="drag", x=20, y=30,
        end_x=850, end_y=430, duration=1.8), approval_id="pause-drag")
    instance.approve(envelope)
    at_interrupt = []
    original_interrupt = instance.driver.interrupt_action

    def inspect_interrupt():
        with sqlite3.connect(instance.path) as db:
            at_interrupt.append((
                db.execute("SELECT status FROM tasks WHERE id=?", (instance.task.id,)).fetchone()[0],
                db.execute("SELECT revoked FROM leases WHERE id=?", (instance.lease.id,)).fetchone()[0],
                db.execute("SELECT revoked FROM approvals WHERE id=?", (envelope.approval_id,)).fetchone()[0],
                db.execute("SELECT state FROM actions WHERE id=?", (envelope.action_id,)).fetchone()[0]))
        original_interrupt()

    monkeypatch.setattr(instance.driver, "interrupt_action", inspect_interrupt)
    running = instance.start(envelope)
    await page.wait_for_function("events.filter(e=>e.kind==='pointermove'&&e.buttons===1).length>=4")
    paused = await asyncio.wait_for(instance.gateway.suspend(instance.task.id, instance.task.revision), 3)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert paused.status == "PAUSED"
    assert at_interrupt[0] == ("PAUSED", 1, 1, "outcome_unknown")
    assert instance.driver._page is page and instance.driver._context is context
    assert not page.is_closed() and not instance.driver._actions
    assert all(not pointer._held for pointer in instance.driver._pointers.values())
    events = (await instance.observed())["events"]
    assert sum(e["kind"] == "pointerdown" for e in events) == 1
    assert sum(e["kind"] == "pointerup" for e in events) == 1
    assert (await instance.observed())["saved"] == 0
    assert instance.driver._motion_interrupted.is_set()
    with pytest.raises(OutcomeUnknown):
        await instance.gateway.resume(paused.id, paused.revision)
    with pytest.raises(AdmissionDenied):
        await instance.gateway.dispatch(envelope)
    with pytest.raises(AdmissionDenied):
        await instance.gateway.observe(paused.id, paused.revision, instance.lease)
    assert await instance.observed() == {"saved": 0, "events": events}


async def test_resume_requires_new_lease_running_revision_frame_and_approval(world):
    instance = await world(approvals=("click",))
    old_lease = instance.lease
    old = instance.envelope(approval_id="before-pause")
    instance.approve(old)
    paused = await instance.gateway.suspend(instance.task.id, instance.task.revision)
    assert await instance.gateway.suspend(paused.id, paused.revision) == paused
    resumed = await instance.gateway.resume(paused.id, paused.revision)
    assert resumed.status == "REOBSERVE"
    assert instance.driver._motion_interrupted.is_set()
    with pytest.raises(AdmissionDenied):
        await instance.gateway.observe(resumed.id, resumed.revision, old_lease)
    instance.lease = instance.store.acquire_lease(resumed.id, instance.scope, ttl_seconds=120)
    assert instance.lease.fencing_token > old_lease.fencing_token
    instance.task = resumed
    instance.frame = await instance.gateway.observe(resumed.id, resumed.revision, instance.lease)
    assert instance.driver._motion_interrupted.is_set(), "REOBSERVE must not rearm input"
    with pytest.raises(AdmissionDenied, match="RUNNING observation"):
        await instance.gateway.dispatch(instance.envelope(action_id="too-early", key="too-early"))
    reobserve_frame = instance.envelope(action_id="old-frame", key="old-frame")
    instance.task = instance.store.transition(resumed.id, resumed.revision, "RUNNING")
    with pytest.raises(AdmissionDenied):
        await instance.gateway.dispatch(reobserve_frame)
    instance.frame = await instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease)
    assert not instance.driver._motion_interrupted.is_set()
    assert instance.frame.reference.task_revision == instance.task.revision
    with pytest.raises((AdmissionDenied, Conflict)):
        await instance.gateway.dispatch(old)
    with pytest.raises(AdmissionDenied, match="approval"):
        await instance.gateway.dispatch(instance.envelope(action_id="old-approval", key="old-approval",
            approval_id=old.approval_id))
    assert (await instance.observed()) == {"saved": 0, "events": []}
    fresh = instance.envelope(action_id="fresh", key="fresh", approval_id="after-pause")
    instance.approve(fresh)
    receipt = await instance.gateway.dispatch(fresh)
    assert receipt["state"] == "succeeded"
    assert (await instance.observed())["saved"] == 1
    assert not instance.store.pending_effects(instance.task.id)


async def test_cancelled_pause_caller_still_drains_slow_observation_without_closing(world):
    instance = await world()
    reading, cancelled, drain = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_transform(snapshot, *, quick):
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await drain.wait()

    instance.gateway._observation_transform = slow_transform
    observing = asyncio.create_task(instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease))
    instance.tasks.append(observing)
    await asyncio.wait_for(reading.wait(), 2)
    pausing = asyncio.create_task(instance.gateway.suspend(instance.task.id, instance.task.revision))
    await asyncio.wait_for(cancelled.wait(), 2)
    pausing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pausing
    paused = instance.store.get_task(instance.task.id)
    assert paused.status == "PAUSED"
    assert not observing.done(), "Pause caller cancellation must not cancel owned draining"
    assert not instance.gateway._suspensions[paused.id].cleanup.done()
    drain.set()
    assert (await asyncio.wait_for(instance.gateway.suspend(paused.id, paused.revision), 2)).status == "PAUSED"
    with pytest.raises(asyncio.CancelledError):
        await observing
    assert not instance.driver._page.is_closed()
    assert instance.driver._context is not None
    assert not instance.store.pending_effects(paused.id)
    assert (await instance.observed()) == {"saved": 0, "events": []}


async def test_stop_wins_same_revision_pause_race_and_does_not_accept_unrelated_stale_stop(world):
    instance = await world()
    reading, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_transform(snapshot, *, quick):
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()

    instance.gateway._observation_transform = slow_transform
    observing = asyncio.create_task(instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease))
    instance.tasks.append(observing)
    await asyncio.wait_for(reading.wait(), 2)
    pausing = asyncio.create_task(instance.gateway.suspend(instance.task.id, instance.task.revision))
    await asyncio.wait_for(cancelling.wait(), 2)
    stopping = asyncio.create_task(instance.gateway.stop(instance.task.id, instance.task.revision))
    release.set()
    paused_result, stopped = await asyncio.wait_for(asyncio.gather(pausing, stopping), 3)
    assert paused_result.status in {"CANCELLING", "CANCELLED"}
    assert stopped.status == "CANCELLED"
    assert instance.driver._context is None
    with pytest.raises(AdmissionDenied):
        await instance.gateway.resume(instance.task.id, instance.task.revision + 1)
    assert (await instance.gateway.suspend(instance.task.id, instance.task.revision)).status == "CANCELLED"
    unrelated = await world()
    unrelated.store.transition(unrelated.task.id, unrelated.task.revision, "REPLAN")
    with pytest.raises(Conflict):
        await unrelated.gateway.stop(unrelated.task.id, unrelated.task.revision)
    assert not unrelated.driver._page.is_closed()


async def test_pause_releases_keys_from_original_inflight_press_without_global_input(world, monkeypatch):
    instance = await world()
    page = instance.driver._page
    pressed = asyncio.Event()
    await page.evaluate("document.addEventListener('keyup',e=>window.lastReleased=e.key)")

    async def pending_press(chord):
        assert chord == "Control+a"
        await page.keyboard.down("Control")
        await page.keyboard.down("a")
        pressed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(page.keyboard, "press", pending_press)
    running = instance.start(instance.envelope(action=Action(type="key", key="CTRL+A")))
    await asyncio.wait_for(pressed.wait(), 2)
    # A page switch while a press is in flight must not redirect its release
    # to the newly selected page. Both are disposable, headless pages.
    other = await instance.driver._context.new_page()
    await other.set_content("<script>window.releases=0;document.onkeyup=()=>releases++</script>")
    instance.driver._page = other
    paused = await asyncio.wait_for(instance.gateway.suspend(instance.task.id, instance.task.revision), 3)
    assert paused.status == "PAUSED"
    with pytest.raises(asyncio.CancelledError):
        await running
    events = await page.evaluate("events")
    assert sum(e["kind"] == "keydown" for e in events) == 2
    assert sum(e["kind"] == "keyup" for e in events) == 2
    assert await page.evaluate("lastReleased") == "Control"
    assert await other.evaluate("releases") == 0
    assert not page.is_closed()
    with pytest.raises(OutcomeUnknown):
        await instance.gateway.resume(paused.id, paused.revision)


async def test_failed_key_release_stays_paused_and_cannot_be_rearmed(world, monkeypatch):
    instance = await world()
    page = instance.driver._page
    pressed = asyncio.Event()

    async def pending_press(chord):
        await page.keyboard.down("Control")
        pressed.set()
        await asyncio.Event().wait()

    original_up = page.keyboard.up

    async def failed_up(key):
        raise RuntimeError("fixture transport release failure")

    monkeypatch.setattr(page.keyboard, "press", pending_press)
    running = instance.start(instance.envelope(action=Action(type="key", key="CTRL+A")))
    await asyncio.wait_for(pressed.wait(), 2)
    monkeypatch.setattr(page.keyboard, "up", failed_up)
    with pytest.raises(DriverAbort, match="fully released"):
        await instance.gateway.suspend(instance.task.id, instance.task.revision)
    with pytest.raises(asyncio.CancelledError):
        await running
    paused = instance.store.get_task(instance.task.id)
    assert paused.status == "PAUSED"
    # Even an explicit parent reconciliation cannot turn failed draining into
    # a safe resume. Stop/close is required to dispose this browser session.
    instance.store.record_outcome("action-save", {"fixture_reconciled": True}, succeeded=False)
    with pytest.raises(DriverAbort):
        await instance.gateway.resume(paused.id, paused.revision)
    assert instance.driver._motion_interrupted.is_set()
    assert not page.is_closed()
    monkeypatch.setattr(page.keyboard, "up", original_up)
    stopped = await instance.gateway.stop(paused.id, paused.revision)
    assert stopped.status == "CANCELLED" and instance.driver._context is None


async def test_pause_during_rearm_cannot_publish_a_frame_or_clear_new_pause(world, monkeypatch):
    instance = await world()
    paused = await instance.gateway.suspend(instance.task.id, instance.task.revision)
    resumed = await instance.gateway.resume(paused.id, paused.revision)
    instance.lease = instance.store.acquire_lease(resumed.id, instance.scope, ttl_seconds=120)
    instance.task = instance.store.transition(resumed.id, resumed.revision, "RUNNING")
    entered = asyncio.Event()
    original_resume = instance.driver.resume_actions

    async def slow_rearm():
        await original_resume()
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(instance.driver, "resume_actions", slow_rearm)
    observing = asyncio.create_task(instance.gateway.observe(instance.task.id, instance.task.revision, instance.lease))
    instance.tasks.append(observing)
    await asyncio.wait_for(entered.wait(), 2)
    assert not instance.driver._motion_interrupted.is_set()
    latest = await asyncio.wait_for(instance.gateway.suspend(instance.task.id, instance.task.revision), 3)
    with pytest.raises(asyncio.CancelledError):
        await observing
    assert latest.status == "PAUSED"
    assert instance.driver._motion_interrupted.is_set()
    assert instance.gateway._browsers[instance.scope.resource_id].frame is None
    assert not instance.driver._page.is_closed()
    assert (await instance.observed()) == {"saved": 0, "events": []}
