"""Candidate authority-store invariants: temporary SQLite, deterministic clocks.

These tests do not run a model, browser, desktop or the legacy Run. Passing them
is partial storage-contract evidence, not an end-to-end runtime/gate approval.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest
from pydantic import ValidationError

from server.core.contracts import (ActionEnvelope, Approval, Budget, Checkpoint,
    CompletionVerdict, Evidence, ObservationRef, ResourceLease, ResourceScope,
    SuccessCriterion, Task)
from server.core.store import AdmissionDenied, Conflict, OutcomeUnknown, TaskStore
from server.schemas import Action


class FakeClock:
    def __init__(self):
        self.now = datetime(2026,9,11,12,0,tzinfo=timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def world(tmp_path):
    clock = FakeClock()
    stores = []
    path = tmp_path/'authority.sqlite3'
    def open_store():
        store=TaskStore(path,clock=clock)
        stores.append(store)
        return store
    yield clock,open_store,path
    for store in stores:
        store.close()


def task_at(store,clock,task_id='task-main',status='RUNNING'):
    store.pin_policy('policy-1', 'a'*64)
    task=store.create_task(Task(id=task_id,goal='Fill the field and save the form',policy_ref='policy-1',
        created_at=clock(),updated_at=clock(),success_criteria=(
            SuccessCriterion(id='field',description='Field has exact requested value',verifier_ref='fixture-verifier'),
            SuccessCriterion(id='saved',description='Saved-state oracle is true',verifier_ref='fixture-verifier'))))
    for state in ('QUEUED','PLANNING','READY','RUNNING','VERIFYING'):
        if task.status==status:break
        task=store.transition(task.id,task.revision,state)
    return task


def scope(resource='browser-main',session='session-main'):
    return ResourceScope(resource_id=resource,kind='browser',session_id=session,
                         website_origin='https://fixture.invalid')


def observation(store,clock,task,resource=None,*,obs_id='obs-main',revision=1,ttl=20,offset=0):
    value=ObservationRef(id=obs_id,task_id=task.id,task_revision=task.revision,revision=revision,
        observed_at=clock()+timedelta(seconds=offset),expires_at=clock()+timedelta(seconds=offset+ttl),
        resource_scope=resource or scope(),frame_version=revision,coordinate_space='screenshot_pixels')
    store.record_observation(value)
    return value


def envelope(task,lease,obs,*,action_id='action-main',approval_id='approval-main',key='idem-main',**changes):
    fields=dict(action_id=action_id,task_id=task.id,task_revision=task.revision,step_id='step-save',
        tool_id='browser.click',tool_version='1.0',action=Action(type='click',target='save-button'),
        observation_id=obs.id,observation_revision=obs.revision,policy_ref=task.policy_ref,
        resource_scope=lease.resource_scope,approval_id=approval_id,lease_id=lease.id,
        fencing_token=lease.fencing_token,idempotency_key=key,created_at=obs.observed_at)
    fields.update(changes)
    return ActionEnvelope.create(**fields)


def approve(store,clock,action,*,ttl=15,offset=0):
    value=Approval(id=action.approval_id,task_id=action.task_id,task_revision=action.task_revision,
        action_id=action.action_id,payload_sha256=action.payload_sha256,resource_scope=action.resource_scope,
        principal='local-user',destination='https://fixture.invalid',issued_at=clock()+timedelta(seconds=offset),
        expires_at=clock()+timedelta(seconds=offset+ttl))
    store.approve(value)
    return value


def ready_action(store,clock,*,task_id='task-main',obs_ttl=20,approval_ttl=15):
    task=task_at(store,clock,task_id)
    lease=store.acquire_lease(task.id,scope(),ttl_seconds=30)
    obs=observation(store,clock,task,ttl=obs_ttl,obs_id='obs-'+task_id)
    action=envelope(task,lease,obs,action_id='action-'+task_id,approval_id='approval-'+task_id,key='idem-'+task_id)
    approval=approve(store,clock,action,ttl=approval_ttl)
    return task,lease,obs,action,approval


def evidence(clock,task,obs,*,evidence_id='evidence-main',criteria=('field','saved'),source='external_verifier',verifier='fixture-verifier',result='pass',**changes):
    fields=dict(id=evidence_id,task_id=task.id,task_revision=task.revision,observation_id=obs.id,
        observation_revision=obs.revision,source=source,observed_at=clock(),check_type='fixture-state',
        result=result,summary='Independent fixture state readback',verifier_ref=verifier,criterion_ids=criteria)
    fields.update(changes)
    return Evidence(**fields)


def verdict(clock,task,*evidence_ids,criteria=('field','saved'),verifier='fixture-verifier'):
    return CompletionVerdict(verifier_ref=verifier,verdict='succeeded',task_revision=task.revision,
        evidence_ids=evidence_ids or ('evidence-main',),criterion_ids=criteria,decided_at=clock())


def checkpoint(clock,task,*,effects=(),identifier='checkpoint-main',evidence_ids=()):
    return Checkpoint(id=identifier,task_id=task.id,task_revision=task.revision,
        plan_version=task.plan_version,created_at=clock(),verified_evidence_ids=evidence_ids,
        pending_effects=effects,budget_remaining=Budget(max_steps=10,max_seconds=60.0),policy_digest='a'*64)


def test_task_and_ordered_event_history_survive_reopen(world):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock)
    before=store.events(task.id)
    store.close();reopened=open_store()
    assert reopened.get_task(task.id)==task
    assert reopened.list_tasks()==[task]
    assert reopened.events(task.id)==before
    assert [event.sequence for event in before]==list(range(1,len(before)+1))


def test_checkpoint_cannot_replace_the_persistently_pinned_policy(world):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock)
    wrong=checkpoint(clock,task).model_copy(update={'policy_digest':'b'*64})
    with pytest.raises(AdmissionDenied,match='policy digest'):
        store.checkpoint(wrong)
    assert store.latest_checkpoint(task.id) is None
    store.checkpoint(checkpoint(clock,task))
    assert open_store().latest_checkpoint(task.id).policy_digest=='a'*64


def test_unpinned_policy_cannot_admit_input_or_save_a_checkpoint(world):
    clock,open_store,path=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    with sqlite3.connect(path) as legacy:
        legacy.execute('DELETE FROM policies')  # Simulate candidate v2 metadata.
    with pytest.raises(AdmissionDenied,match='policy'):
        store.admit_action(action)
    with pytest.raises(AdmissionDenied,match='policy'):
        store.checkpoint(checkpoint(clock,task))
    assert store.pending_effects(task.id)==[]
    store.pin_policy(task.policy_ref,'a'*64)  # No dispatch exists; parent can pin.
    assert store.admit_action(action)['dispatch'] is True


def test_driver_boundary_checks_revocation_committed_by_another_connection(world):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    store.admit_action(action)
    store.assert_dispatch_authority(action)
    other=open_store()
    other.release_lease(lease.id,lease.fencing_token)
    with pytest.raises(AdmissionDenied,match='revoked'):
        store.assert_dispatch_authority(action)
    assert store.pending_effects(task.id)[0]['state']=='outcome_unknown'


def test_stale_revision_rejected_without_state_or_event_change(world):
    clock,open_store,_=world
    first=open_store();second=open_store()
    task=task_at(first,clock,status='PLANNING')
    first.transition(task.id,task.revision,'READY')
    before=first.events(task.id)
    with pytest.raises(Conflict):second.transition(task.id,task.revision,'PAUSED')
    assert second.get_task(task.id).status=='READY'
    assert second.events(task.id)==before


@pytest.mark.parametrize('state',['DRAFT','RUNNING','VERIFYING'])
def test_no_ordinary_transition_can_mark_success(world,state):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status=state)
    before=store.events(task.id)
    with pytest.raises(AdmissionDenied):store.transition(task.id,task.revision,'SUCCEEDED')
    assert store.get_task(task.id)==task and store.events(task.id)==before


def test_two_independent_stores_exclude_same_resource_and_persist_fence(world):
    clock,open_store,_=world
    stores=[open_store(),open_store()]
    tasks=[task_at(stores[0],clock,'task-a'),task_at(stores[1],clock,'task-b')]
    barrier=threading.Barrier(2)
    def acquire(index):
        barrier.wait(timeout=2)
        try:return stores[index].acquire_lease(tasks[index].id,scope())
        except Conflict as error:return error
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(acquire,[0,1]))
    assert sum(isinstance(result,ResourceLease) for result in results)==1
    assert sum(isinstance(result,Conflict) for result in results)==1
    winner=next(result for result in results if isinstance(result,ResourceLease))
    assert winner.fencing_token==1
    stores[0].release_lease(winner.id,winner.fencing_token)
    second=stores[1].acquire_lease(tasks[1].id,scope())
    assert second.fencing_token==2
    stores[1].release_lease(second.id,second.fencing_token)
    stores[0].close();stores[1].close()
    assert open_store().acquire_lease(tasks[0].id,scope()).fencing_token==3


def test_exact_approval_admits_once_and_bad_payload_does_not_consume_it(world):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    changed=envelope(task,lease,obs,action_id=action.action_id,approval_id=action.approval_id,
                     key=action.idempotency_key,action=Action(type='click',target='different-button'))
    with pytest.raises(AdmissionDenied):store.admit_action(changed)
    assert not any(e.type=='action.dispatched' for e in store.events(task.id))
    assert store.admit_action(action)=={'dispatch':True,'state':'dispatched'}
    with pytest.raises(OutcomeUnknown):store.admit_action(action)


@pytest.mark.parametrize('mode',['missing','wrong_action','expired','future'])
def test_approval_must_be_exact_and_current(world,mode):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock)
    lease=store.acquire_lease(task.id,scope());obs=observation(store,clock,task,ttl=60)
    action=envelope(task,lease,obs)
    if mode=='wrong_action':
        other=envelope(task,lease,obs,action_id='another-action')
        approve(store,clock,other)
    elif mode=='expired':
        approve(store,clock,action,ttl=2);clock.advance(2)
    elif mode=='future':approve(store,clock,action,offset=2)
    with pytest.raises(AdmissionDenied):store.admit_action(action)
    assert not any(e.type=='action.dispatched' for e in store.events(task.id))


@pytest.mark.parametrize('mode',['expired','superseded','future','wrong_resource','old_fence'])
def test_stale_observation_or_lease_cannot_authorize_input(world,mode):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock,obs_ttl=2,approval_ttl=25)
    if mode=='expired':clock.advance(2)
    elif mode=='superseded':observation(store,clock,task,obs_id='obs-new',revision=2)
    elif mode=='future':
        new=observation(store,clock,task,obs_id='obs-future',revision=2,offset=5)
        action=envelope(task,lease,new,approval_id=None)
    elif mode=='wrong_resource':action=envelope(task,lease,obs,approval_id=None,resource_scope=scope(session='different-session'))
    else:
        store.release_lease(lease.id,lease.fencing_token)
        newer=store.acquire_lease(task.id,scope())
        action=envelope(task,newer,obs,approval_id=None,fencing_token=lease.fencing_token)
    with pytest.raises(AdmissionDenied):store.admit_action(action,require_approval=mode in {'expired','superseded'})


def test_unrecorded_dispatch_stays_unknown_across_restart_and_cannot_change_id(world):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    assert store.admit_action(action)['dispatch']
    store.close();reopened=open_store()
    with pytest.raises(OutcomeUnknown):reopened.admit_action(action)
    changed_id=envelope(task,lease,obs,action_id='new-attempt',key='new-idempotency',approval_id=None)
    with pytest.raises(OutcomeUnknown):reopened.admit_action(changed_id,require_approval=False)
    assert len(reopened.pending_effects(task.id))==1
    assert sum(e.type=='action.dispatched' for e in reopened.events(task.id))==1
    reopened.record_outcome(action.action_id,{'actual_saved':True},succeeded=True)
    assert reopened.admit_action(action)=={'dispatch':False,'state':'succeeded','outcome':{'actual_saved':True}}
    with pytest.raises(Conflict):reopened.record_outcome(action.action_id,{'actual_saved':False},succeeded=False)


def test_stop_atomically_revokes_lease_approval_and_retains_unknown_effect(world):
    clock,open_store,path=world
    store=open_store();task,lease,obs,action,approval=ready_action(store,clock)
    store.admit_action(action)
    stopped=store.request_stop(task.id,task.revision)
    assert stopped.status=='CANCELLING' and stopped.revision==task.revision+1
    assert store.pending_effects(task.id)[0]['state']=='outcome_unknown'
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT revoked FROM leases WHERE id=?',(lease.id,)).fetchone()==(1,)
        assert db.execute('SELECT revoked FROM approvals WHERE id=?',(approval.id,)).fetchone()==(1,)
    with pytest.raises(Conflict):store.admit_action(action)
    with pytest.raises(AdmissionDenied):store.acquire_lease(task.id,scope())
    cancelled=store.transition(task.id,stopped.revision,'CANCELLED')
    assert cancelled.status=='CANCELLED' and store.pending_effects(task.id)


def test_verifier_requires_all_criteria_and_success_persists(world):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status='VERIFYING')
    lease=store.acquire_lease(task.id,scope());obs=observation(store,clock,task)
    store.record_evidence(evidence(clock,task,obs,evidence_id='ev-field',criteria=('field',)))
    with pytest.raises(AdmissionDenied):store.commit_verdict(task.id,task.revision,verdict(clock,task,'ev-field'))
    with pytest.raises(AdmissionDenied):store.commit_verdict(task.id,task.revision,verdict(clock,task,'ev-field',criteria=('field',)))
    store.record_evidence(evidence(clock,task,obs,evidence_id='ev-saved',criteria=('saved',)))
    done=store.commit_verdict(task.id,task.revision,verdict(clock,task,'ev-field','ev-saved'))
    assert done.status=='SUCCEEDED' and done.completion.actor=='external_verifier'
    store.close();reopened=open_store()
    assert reopened.get_task(task.id)==done
    other=task_at(reopened,clock,'task-next')
    assert reopened.acquire_lease(other.id,scope()).fencing_token>lease.fencing_token


@pytest.mark.parametrize('source,result,verifier',[
    ('planner','pass','fixture-verifier'),('external_verifier','fail','fixture-verifier'),
    ('external_verifier','inconclusive','fixture-verifier'),('external_verifier','pass','unapproved-verifier'),
    ('tool','pass','fixture-verifier'),('observer','pass','fixture-verifier'),('user','pass','fixture-verifier')])
def test_planner_failed_or_wrong_verifier_evidence_cannot_claim_success(world,source,result,verifier):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status='VERIFYING');obs=observation(store,clock,task)
    store.record_evidence(evidence(clock,task,obs,source=source,result=result,verifier=verifier))
    with pytest.raises(AdmissionDenied):store.commit_verdict(task.id,task.revision,verdict(clock,task,verifier=verifier))
    assert store.get_task(task.id).status=='VERIFYING'


@pytest.mark.parametrize('mode',['missing_observation','foreign_observation','wrong_observation_revision','future_evidence'])
def test_evidence_must_bind_recorded_observation_and_valid_time(world,mode):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status='VERIFYING');obs=observation(store,clock,task)
    changes={}
    if mode=='missing_observation':changes['observation_id']='not-recorded'
    elif mode=='wrong_observation_revision':changes['observation_revision']=99
    elif mode=='future_evidence':changes['observed_at']=clock()+timedelta(seconds=1)
    else:
        foreign=task_at(store,clock,'other-task',status='VERIFYING')
        other_obs=observation(store,clock,foreign,obs_id='other-observation')
        changes['observation_id']=other_obs.id
    with pytest.raises(AdmissionDenied):store.record_evidence(evidence(clock,task,obs,**changes))
    assert store.get_task(task.id).status=='VERIFYING'


@pytest.mark.parametrize('mode',['expired','newer_observation'])
def test_completion_rechecks_evidence_freshness(world,mode):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status='VERIFYING');obs=observation(store,clock,task,ttl=2)
    store.record_evidence(evidence(clock,task,obs))
    if mode=='expired':clock.advance(2)
    else:observation(store,clock,task,obs_id='new-observation',revision=2)
    with pytest.raises(AdmissionDenied):store.commit_verdict(task.id,task.revision,verdict(clock,task))


def test_checkpoint_cannot_omit_unknown_dispatch_and_reopen_never_replays(world):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    store.admit_action(action)
    with pytest.raises(AdmissionDenied):store.checkpoint(checkpoint(clock,task))
    pending=store.pending_effects(task.id)
    saved=checkpoint(clock,task,effects=pending)
    store.checkpoint(saved);store.close();reopened=open_store()
    restored=reopened.latest_checkpoint(task.id)
    assert restored==saved
    assert restored.resume_rules.reobserve_required and restored.resume_rules.reconcile_required
    assert restored.resume_rules.replay_pending_actions=='never'
    assert restored.pending_effects[0].state=='outcome_unknown'
    with pytest.raises(OutcomeUnknown):reopened.admit_action(action)


def test_checkpoint_rejects_fabricated_effect_and_mismatched_pending_hash(world):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    fake={'action_id':action.action_id,'payload_sha256':action.payload_sha256,
          'idempotency_key':action.idempotency_key,'state':'outcome_unknown'}
    with pytest.raises(AdmissionDenied):store.checkpoint(checkpoint(clock,task,effects=[fake]))
    store.admit_action(action)
    with pytest.raises(AdmissionDenied):store.checkpoint(checkpoint(clock,task,effects=[fake|{'payload_sha256':'b'*64}]))


def test_unknown_resource_effect_blocks_another_task_after_lease_expiry(world):
    """A new task ID must not bypass resource-level crash reconciliation."""
    clock,open_store,_=world
    first=open_store();task,lease,obs,action,_=ready_action(first,clock)
    first.admit_action(action)
    first.close()
    clock.advance(31)
    second=open_store()
    other=task_at(second,clock,'recovery-as-new-task')
    new_lease=second.acquire_lease(other.id,scope())
    fresh=observation(second,clock,other,obs_id='recovery-observation')
    new_action=envelope(other,new_lease,fresh,action_id='recovery-action',approval_id='recovery-approval',key='recovery-idem')
    approve(second,clock,new_action)
    with pytest.raises(OutcomeUnknown):
        second.admit_action(new_action)


def test_success_requires_reconciliation_of_all_dispatched_effects(world):
    clock,open_store,_=world
    store=open_store();task,lease,old_obs,action,_=ready_action(store,clock)
    store.admit_action(action)
    verifying=store.transition(task.id,task.revision,'VERIFYING')
    fresh=observation(store,clock,verifying,obs_id='verification-observation',revision=2)
    store.record_evidence(evidence(clock,verifying,fresh))
    with pytest.raises(OutcomeUnknown):
        store.commit_verdict(task.id,verifying.revision,verdict(clock,verifying))
    assert store.get_task(task.id).status=='VERIFYING'
    assert len(store.pending_effects(task.id))==1
    store.record_outcome(action.action_id,{'independent_reconciliation':'saved and settled'},succeeded=True)
    assert store.commit_verdict(task.id,verifying.revision,verdict(clock,verifying)).status=='SUCCEEDED'


def test_checkpoint_cannot_launder_planner_claim_as_verified_evidence(world):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock);obs=observation(store,clock,task)
    store.record_evidence(evidence(clock,task,obs,source='planner'))
    with pytest.raises(AdmissionDenied):
        store.checkpoint(checkpoint(clock,task,evidence_ids=('evidence-main',)))
    assert store.latest_checkpoint(task.id) is None


@pytest.mark.parametrize('changes',[{}, {'website_origin':'https://different.invalid'}, {'account_id':'different-account'}])
def test_resource_alias_cannot_split_one_browser_input_session(world,changes):
    """Resource labels, URLs and accounts must not create a second input lock."""
    clock,open_store,_=world
    first=open_store();second=open_store()
    task_a=task_at(first,clock,'task-alias-a');task_b=task_at(second,clock,'task-alias-b')
    first.acquire_lease(task_a.id,scope(resource='resource-alias-a',session='one-browser-session'))
    alias=scope(resource='resource-alias-b',session='one-browser-session').model_copy(update=changes)
    with pytest.raises((Conflict,AdmissionDenied)):
        second.acquire_lease(task_b.id,alias)


def test_different_browser_sessions_keep_independent_resource_leases(world):
    clock,open_store,_=world
    store=open_store()
    task_a=task_at(store,clock,'independent-a');task_b=task_at(store,clock,'independent-b')
    first=store.acquire_lease(task_a.id,scope(resource='browser-a',session='session-a'))
    second=store.acquire_lease(task_b.id,scope(resource='browser-b',session='session-b'))
    assert first.owner!=second.owner
    assert first.resource_scope.session_id!=second.resource_scope.session_id


@pytest.mark.parametrize('timing',['future','before_evidence'])
def test_verdict_time_cannot_precede_evidence_or_exceed_current_clock(world,timing):
    clock,open_store,_=world
    store=open_store();task=task_at(store,clock,status='VERIFYING')
    obs=observation(store,clock,task)
    clock.advance(2)
    store.record_evidence(evidence(clock,task,obs))
    decision=verdict(clock,task).model_copy(update={
        'decided_at':clock()+timedelta(seconds=1 if timing=='future' else -1)})
    before=store.events(task.id)
    with pytest.raises(AdmissionDenied):
        store.commit_verdict(task.id,task.revision,decision)
    assert store.get_task(task.id).status=='VERIFYING'
    assert store.events(task.id)==before
    assert store.commit_verdict(task.id,task.revision,verdict(clock,task)).status=='SUCCEEDED'


@pytest.mark.parametrize('consumed',[0,1])
def test_checkpoint_cannot_inflate_original_or_consumed_step_budget(world,consumed):
    clock,open_store,_=world
    store=open_store();task,lease,obs,action,_=ready_action(store,clock)
    if consumed:
        store.admit_action(action)
        store.record_outcome(action.action_id,{'actual_saved':True},succeeded=True)
    remaining=task.budget.max_steps-consumed
    proposed=checkpoint(clock,task).model_copy(update={
        'budget_remaining':Budget(max_steps=remaining+1)})
    with pytest.raises(AdmissionDenied):
        store.checkpoint(proposed)
    assert store.latest_checkpoint(task.id) is None
    valid=proposed.model_copy(update={'budget_remaining':Budget(max_steps=remaining)})
    store.checkpoint(valid)
    assert store.latest_checkpoint(task.id).budget_remaining.max_steps==remaining
