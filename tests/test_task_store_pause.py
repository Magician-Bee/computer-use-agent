"""Atomic pause authority and resume prerequisites; no external input."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from server.core.store import AdmissionDenied, Conflict, OutcomeUnknown
from test_task_store import world, ready_action, scope


def test_pause_revokes_input_and_approval_then_resume_requires_new_authority(world):
    clock, open_store, _ = world
    store = open_store()
    task, lease, _, action, approval = ready_action(store, clock)
    paused = store.request_pause(task.id, task.revision)
    assert paused.status == "PAUSED" and paused.revision == task.revision + 1
    assert store._db.execute("SELECT revoked FROM leases WHERE id=?", (lease.id,)).fetchone()[0] == 1
    assert store._db.execute("SELECT revoked FROM approvals WHERE id=?", (approval.id,)).fetchone()[0] == 1
    assert store.request_pause(task.id, paused.revision) == paused
    assert sum(e.type == "task.pause_requested" for e in store.events(task.id)) == 1
    with pytest.raises(Conflict):
        store.admit_action(action)
    resumed = store.request_resume(task.id, paused.revision)
    assert resumed.status == "REOBSERVE" and resumed.revision == paused.revision + 1
    with pytest.raises(AdmissionDenied):
        store.validate_lease(task.id, resumed.revision, scope(), lease.id, lease.fencing_token)
    fresh = store.acquire_lease(task.id, scope())
    assert fresh.fencing_token > lease.fencing_token
    with pytest.raises(Conflict):
        store.admit_action(action)


@pytest.mark.parametrize("pause_method", ["request_pause", "transition"])
def test_unknown_effect_blocks_every_resume_entry_and_survives_reopen(world, pause_method):
    clock, open_store, _ = world
    store = open_store()
    task, _, _, action, _ = ready_action(store, clock)
    store.admit_action(action)
    paused = (store.request_pause(task.id, task.revision) if pause_method == "request_pause"
              else store.transition(task.id, task.revision, "PAUSED"))
    other = open_store()
    assert other.pending_effects(task.id)[0]["action_id"] == action.action_id
    with pytest.raises(OutcomeUnknown):
        other.request_resume(task.id, paused.revision)
    with pytest.raises(OutcomeUnknown):
        other.transition(task.id, paused.revision, "REOBSERVE")
    assert other.get_task(task.id) == paused
    # Trusted outcome recording is simulated here; it is not a real driver
    # reconciliation proof and never resubmits the action.
    other.record_outcome(action.action_id, {"readback": "known_partial_effect"}, succeeded=False)
    assert other.request_resume(task.id, paused.revision).status == "REOBSERVE"
    assert other._db.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 1


def test_stop_wins_over_late_pause_and_stale_resume(world):
    clock, open_store, _ = world
    store = open_store()
    task, *_ = ready_action(store, clock)
    paused = store.request_pause(task.id, task.revision)
    stopped = store.request_stop(task.id, paused.revision)
    assert store.request_pause(task.id, task.revision) == stopped
    with pytest.raises(Conflict):
        store.request_resume(task.id, paused.revision)
    cancelled = store.transition(task.id, stopped.revision, "CANCELLED")
    assert store.request_pause(task.id, paused.revision) == cancelled


def test_cross_connection_admission_race_cannot_leave_dispatch_authorized_after_pause(world):
    clock, open_store, _ = world
    store, other = open_store(), open_store()
    task, lease, _, action, _ = ready_action(store, clock)
    barrier = threading.Barrier(2)

    def admit():
        barrier.wait()
        try:
            return store.admit_action(action)
        except (AdmissionDenied, Conflict):
            return None

    def pause():
        barrier.wait()
        return other.request_pause(task.id, task.revision)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(admit), pool.submit(pause)
        result, paused = a.result(), b.result()
    assert paused.status == "PAUSED"
    with pytest.raises((AdmissionDenied, Conflict)):
        store.assert_dispatch_authority(action)
    assert store._db.execute("SELECT revoked FROM leases WHERE id=?", (lease.id,)).fetchone()[0] == 1
    states = [r[0] for r in store._db.execute("SELECT state FROM actions")]
    assert states == (["outcome_unknown"] if result else [])


def test_pause_resume_failure_rolls_back_state_and_authority(world, monkeypatch):
    clock, open_store, _ = world
    store = open_store()
    task, lease, _, _, _ = ready_action(store, clock)
    original = store._event
    def broken(*_args, **_kwargs):
        raise RuntimeError("Synthetic event journal failure")
    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        store.request_pause(task.id, task.revision)
    assert store.get_task(task.id) == task
    assert store._db.execute("SELECT revoked FROM leases WHERE id=?", (lease.id,)).fetchone()[0] == 0
    monkeypatch.setattr(store, "_event", original)
    paused = store.request_pause(task.id, task.revision)
    monkeypatch.setattr(store, "_event", broken)
    with pytest.raises(RuntimeError):
        store.request_resume(task.id, paused.revision)
    assert store.get_task(task.id) == paused
