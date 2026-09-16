"""Parent-owned browser action boundary; not yet the production Run.

The planner proposes Actions only. Executor registration, policies, leases,
envelopes and approvals are parent responsibilities. This Python boundary is
not an OS sandbox against arbitrary code running in the parent process.
"""
from __future__ import annotations

import asyncio
import base64
import copy
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import inspect
from pathlib import Path
from typing import Awaitable, Callable
import uuid

from server.drivers import BrowserDriver, DriverAbort, _keys
from server.grounding import remap_approved_action, StaleObservation
from server.key_contract import canonical_key_chord
from server.schemas import Action
from .contracts import (ActionEnvelope, ObservationRef, ResourceLease, ResourceScope,
                        ToolIntegrity, ToolManifest, ToolProvenance, utc_now)
from .policy import PolicyRegistry, origin_identity
from .store import AdmissionDenied, Conflict, TaskStore, TERMINAL


def browser_implementation_digest() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256(b"computeruse.browser-implementation.v1\0")
    for name in ("drivers.py", "browser_pointer.py", "motion.py", "browser_text.py", "key_contract.py"):
        content = (root / name).read_bytes()
        digest.update(name.encode()+b"\0"+hashlib.sha256(content).digest())
    return digest.hexdigest()


def browser_manifest(scope: ResourceScope, action_types: tuple[str, ...]) -> ToolManifest:
    digest = browser_implementation_digest()
    return ToolManifest(tool_id="browser.input", version="1.0", action_types=action_types,
        input_schema=Action.model_json_schema(), resource_scopes=(scope,), permissions=("browser.input",),
        side_effects="unknown", sandbox_profile="headless_chromium",
        provenance=ToolProvenance(source="local:server/drivers.py", version_ref=digest),
        integrity=ToolIntegrity(expected_sha256=digest, observed_sha256=digest,
                               verified_at=utc_now(), verifier_ref="parent-code-digest"))


@dataclass(frozen=True)
class ObservedFrame:
    reference: ObservationRef
    snapshot: dict


ObservationTransform = Callable[..., Awaitable[dict]]
_PERCEPTION_FIELDS = frozenset({"text", "elements", "perception"})


@dataclass
class _BrowserBinding:
    owner: str
    scope: ResourceScope
    manifest_json: str
    driver: BrowserDriver
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: ActionEnvelope | None = None
    input_page: object | None = None
    frame: ObservedFrame | None = None
    running: asyncio.Task | None = None
    closing: bool = False
    suspended: bool = False
    needs_rearm: bool = False


@dataclass(frozen=True)
class _Suspension:
    requested_revision: int
    paused_revision: int
    cleanup: asyncio.Task


class ActionGateway:
    def __init__(self, store: TaskStore, policies: PolicyRegistry, *,
                 observation_transform: ObservationTransform | None = None):
        """The optional async transform is trusted parent perception code.

        It receives a private snapshot copy and keyword-only ``quick``. Only
        text/elements/perception may change; screenshot and executor authority
        metadata remain driver-owned. This is not a Python callback sandbox.
        """
        if observation_transform is not None and not (
                inspect.iscoroutinefunction(observation_transform)
                or inspect.iscoroutinefunction(getattr(observation_transform, "__call__", None))):
            raise TypeError("Observation transform must be a trusted async callable")
        self.store, self.policies = store, policies
        self._observation_transform = observation_transform
        self._browsers: dict[str, _BrowserBinding] = {}
        self._stops: dict[str, asyncio.Task] = {}
        self._suspensions: dict[str, _Suspension] = {}

    def register_browser(self, task_id: str, scope: ResourceScope, driver: BrowserDriver, manifest: ToolManifest):
        """Called by the trusted parent, never by the model or its tool payload."""
        scope = ResourceScope.model_validate_json(scope.model_dump_json())
        manifest = ToolManifest.model_validate_json(manifest.model_dump_json())
        task = self.store.get_task(task_id)
        if task.status in TERMINAL | {"CANCELLING"}:
            raise AdmissionDenied("A stopped task cannot register an executor")
        if scope.kind != "browser" or not isinstance(driver, BrowserDriver) or driver.headless is not True:
            raise AdmissionDenied("Only an agent-owned headless browser is currently certified for this gateway")
        if driver._context is None or driver._page is None:
            raise AdmissionDenied("The parent must start its isolated browser before registration")
        if scope.website_origin is not None:
            configured = getattr(driver, "allowed_origin", None)
            if configured is None or origin_identity(configured) != origin_identity(scope.website_origin):
                raise AdmissionDenied("An origin-scoped browser must enforce that origin before startup")
        if (manifest.tool_id != "browser.input" or manifest.version != "1.0" or manifest.resource_scopes != (scope,)
                or manifest.permissions != ("browser.input",) or manifest.sandbox_profile != "headless_chromium"):
            raise AdmissionDenied("Manifest does not bind this exact browser executor")
        if not manifest.integrity.verified or manifest.integrity.expected_sha256 != browser_implementation_digest():
            raise AdmissionDenied("Browser implementation does not match its registered integrity digest")
        if (scope.resource_id in self._browsers or any(
                item.driver is driver or (item.scope.device_id, item.scope.session_id) == (scope.device_id, scope.session_id)
                or (driver._context is not None and item.driver._context is driver._context)
                for item in self._browsers.values())):
            raise Conflict("This executor already has a canonical browser registration")
        policy_digest = self.policies.digest_for(task.policy_ref)
        self.store.pin_policy(task.policy_ref, policy_digest)
        binding = _BrowserBinding(task_id, scope, manifest.model_dump_json(), driver)
        def guard():
            if (binding.closing or binding.suspended or binding.needs_rearm
                    or binding.active is None or asyncio.current_task() is not binding.running):
                raise AdmissionDenied("No active gateway dispatch permits browser input")
            self.store.assert_policy(binding.active.policy_ref, policy_digest)
            self.store.assert_dispatch_authority(binding.active)
            self._check_page_scope(binding)
        driver.bind_action_guard(guard)
        self._browsers[scope.resource_id] = binding

    def _binding(self, scope: ResourceScope):
        binding = self._browsers.get(scope.resource_id)
        if binding is None or binding.scope != scope or binding.closing or binding.suspended:
            raise AdmissionDenied("No live executor is registered for this exact resource")
        return binding

    @staticmethod
    def _check_page_scope(binding):
        if binding.scope.website_origin is None:
            return
        page = binding.driver._page
        # A checked new_tab command creates an intermediate about:blank page.
        # It may only navigate to the explicit origin-approved URL in that same
        # active dispatch; the next independent action cannot use this exception.
        if (page is not None and not page.is_closed() and page.url == "about:blank"
                and binding.active is not None and binding.active.action.type == "new_tab"
                and binding.active.action.url
                and origin_identity(binding.active.action.url) == origin_identity(binding.scope.website_origin)):
            return
        if page is None or page.is_closed() or origin_identity(page.url) != origin_identity(binding.scope.website_origin):
            raise AdmissionDenied("Current browser page is outside the registered origin")

    async def _capture(self, binding: _BrowserBinding, *, quick: bool):
        # Track reads too: stop must be able to cancel slow perception before
        # waiting for this binding's lock to drain. Input still requires active.
        running = asyncio.current_task()
        binding.running = running
        try:
            captured = self.store.clock()
            snapshot = await binding.driver.observe()
            capture_finished = self.store.clock()
            frame_version = binding.driver._document_counter
            snapshot = copy.deepcopy(snapshot)
            snapshot.setdefault("target", "browser")
            snapshot.setdefault("_grounding_text", snapshot.get("text", ""))
            if capture_finished < captured:
                raise AdmissionDenied("Observation clock moved backwards")
            if snapshot.get("physical_input_untouched") is not True:
                raise AdmissionDenied("Executor did not confirm independent browser input")
            if self._observation_transform is not None:
                enriched = await self._observation_transform(copy.deepcopy(snapshot), quick=quick)
                if not isinstance(enriched, dict):
                    raise AdmissionDenied("Observation transform did not return a snapshot")
                fixed = (set(snapshot) | set(enriched)) - _PERCEPTION_FIELDS
                if any(key not in snapshot or key not in enriched or snapshot[key] != enriched[key] for key in fixed):
                    raise AdmissionDenied("Observation transform changed driver-owned authority metadata")
                snapshot = copy.deepcopy(enriched)
            if binding.closing or binding.suspended:
                raise AdmissionDenied("Executor was stopped or paused during observation")
            # Time belongs to raw capture, never to completion of slow OCR.
            return snapshot, captured, frame_version
        finally:
            if binding.running is running:
                binding.running = None

    async def observe(self, task_id: str, expected_revision: int, lease: ResourceLease, *,
                      ttl_seconds=30, quick: bool = False) -> ObservedFrame:
        if not 0 < ttl_seconds <= 120:
            raise ValueError("Observation validity must be within 120 seconds")
        lease = ResourceLease.model_validate_json(lease.model_dump_json())
        binding = self._binding(lease.resource_scope)
        if binding.owner != task_id:
            raise AdmissionDenied("This executor belongs to another task")
        async with binding.lock:
            self._binding(binding.scope)
            self.store.validate_lease(task_id, expected_revision, binding.scope, lease.id, lease.fencing_token)
            self._check_page_scope(binding)
            snapshot, captured, frame_version = await self._capture(binding, quick=quick)
            self.store.validate_lease(task_id, expected_revision, binding.scope, lease.id, lease.fencing_token)
            self._check_page_scope(binding)
            if not captured <= self.store.clock() < captured + timedelta(seconds=ttl_seconds):
                raise AdmissionDenied("Observation expired during capture or perception")
            identifier = uuid.uuid4().hex
            image = snapshot.get("image")
            image_digest = hashlib.sha256(base64.b64decode(image.split(",", 1)[1], validate=True)).hexdigest() if image else None
            reference = ObservationRef(id=identifier, task_id=task_id, task_revision=expected_revision,
                revision=self.store.latest_observation_revision(task_id, binding.scope.resource_id)+1,
                observed_at=captured, expires_at=captured+timedelta(seconds=ttl_seconds),
                resource_scope=binding.scope, frame_version=frame_version,
                coordinate_space="screenshot_pixels", image_ref="volatile:"+identifier if image else None,
                image_digest=image_digest)
            # REOBSERVE reads are permitted while the driver remains interrupted.
            # Only a fresh RUNNING revision, lease and captured frame can rearm
            # input. An ordinary read must never undo a concurrent pause/stop.
            if binding.needs_rearm and self.store.get_task(task_id).status == "RUNNING":
                running = asyncio.current_task()
                binding.running = running
                try:
                    await binding.driver.resume_actions()
                    self._binding(binding.scope)
                    self.store.validate_lease(task_id, expected_revision, binding.scope, lease.id, lease.fencing_token)
                    self._check_page_scope(binding)
                    if self.store.get_task(task_id).status != "RUNNING":
                        raise AdmissionDenied("Input cannot rearm outside RUNNING")
                    if not captured <= self.store.clock() < captured + timedelta(seconds=ttl_seconds):
                        raise AdmissionDenied("Observation expired while rearming input")
                except BaseException:
                    binding.driver.interrupt_action()
                    raise
                finally:
                    if binding.running is running:
                        binding.running = None
            try:
                self.store.record_observation(reference)
                binding.driver.set_observation(copy.deepcopy(snapshot))
                binding.frame = ObservedFrame(reference, copy.deepcopy(snapshot))
            except BaseException:
                if binding.needs_rearm:
                    binding.driver.interrupt_action()
                raise
            if binding.needs_rearm and self.store.get_task(task_id).status == "RUNNING":
                binding.needs_rearm = False
            return ObservedFrame(reference, copy.deepcopy(snapshot))

    async def dispatch(self, envelope: ActionEnvelope) -> dict:
        envelope = ActionEnvelope.model_validate_json(envelope.model_dump_json())
        binding = self._binding(envelope.resource_scope)
        if binding.owner != envelope.task_id:
            raise AdmissionDenied("This executor belongs to another task")
        async with binding.lock:
            self._binding(binding.scope)
            if binding.needs_rearm:
                raise AdmissionDenied("A fresh RUNNING observation is required after resume")
            previous = self.store.existing_action_result(envelope)
            if previous is not None:
                return previous
            manifest = ToolManifest.model_validate_json(binding.manifest_json)
            if manifest.integrity.expected_sha256 != browser_implementation_digest():
                raise AdmissionDenied("Registered browser implementation changed")
            decision = self.policies.evaluate(envelope, manifest)
            self.store.assert_policy(envelope.policy_ref, decision.policy_digest)
            frame = binding.frame
            if (frame is None or frame.reference.id != envelope.observation_id
                    or frame.reference.task_id != envelope.task_id
                    or frame.reference.task_revision != envelope.task_revision
                    or frame.reference.revision != envelope.observation_revision):
                raise AdmissionDenied("A fresh observation from this executor is required")
            self.store.validate_lease(envelope.task_id, envelope.task_revision, binding.scope,
                                      envelope.lease_id, envelope.fencing_token)
            self._check_page_scope(binding)
            if envelope.action.type == "key":
                canonical_key_chord(envelope.action.key, frame.snapshot)
            # Read back the current page before consuming approval or journaling.
            # A changed semantic target must be proposed and authorized anew.
            current, _, _ = await self._capture(binding, quick=True)
            self.store.validate_lease(envelope.task_id, envelope.task_revision, binding.scope,
                                      envelope.lease_id, envelope.fencing_token)
            self._check_page_scope(binding)
            remapped = remap_approved_action(envelope.action, frame.snapshot, current)
            if remapped.model_dump() != envelope.action.model_dump():
                raise StaleObservation("Target identity changed; acquire a fresh observation")
            binding.driver.set_observation(copy.deepcopy(current))
            admitted = self.store.admit_action(envelope, require_approval=decision.require_approval)
            if not admitted["dispatch"]:
                return admitted
            binding.active, binding.running = envelope, asyncio.current_task()
            binding.input_page = binding.driver._page
            try:
                self.store.assert_dispatch_authority(envelope)
                await binding.driver.prepare_for_action()
                await binding.driver.execute(envelope.action)
                # This receipt means the driver returned. Task success requires
                # a separately registered verifier reading independent evidence.
                outcome = {"executor_returned": True, "task_completion_verified": False,
                           "input_transport": "playwright_page_mouse", "physical_input_untouched": True}
                self.store.record_outcome(envelope.action_id, outcome, succeeded=True)
                return {"dispatch": True, "state": "succeeded", "outcome": outcome}
            except BaseException:
                self.store.mark_outcome_unknown(envelope.action_id)
                raise
            finally:
                binding.active = binding.running = binding.frame = binding.input_page = None

    async def suspend(self, task_id: str, expected_revision: int):
        """Pause authority before draining input; preserve the owned browser.

        The durable journal marks interrupted dispatches outcome_unknown. Neither
        this method nor resume retries them or infers whether they took effect.
        Chromium commands already sent cannot be retracted; releases are cleanup.
        """
        paused = self.store.request_pause(task_id, expected_revision)
        if paused.status != "PAUSED":
            # Stop/terminal wins, including when its transaction raced this CAS.
            return await self.stop(task_id, paused.revision)
        previous = self._suspensions.get(task_id)
        if previous is None or previous.paused_revision != paused.revision:
            for binding in self._browsers.values():
                if binding.owner == task_id:
                    binding.suspended = binding.needs_rearm = True
                    binding.frame = None
            cleanup = asyncio.create_task(self._suspend_owned(task_id))
            self._suspensions[task_id] = _Suspension(expected_revision, paused.revision, cleanup)
        return await asyncio.shield(self._suspensions[task_id].cleanup)

    async def _suspend_owned(self, task_id):
        owned = []
        for binding in self._browsers.values():
            if binding.owner != task_id:
                continue
            running, active = binding.running, binding.active
            # keyboard.press is one browser IPC, without Python-held-key state.
            # Release its possible keys on the original page after draining it.
            # This does not assert atomic cancellation inside Chromium.
            keys = (_keys(active.action.key or "", browser=True)
                    if active is not None and active.action.type == "key" else [])
            owned.append((binding, running, binding.input_page, keys))
            binding.driver.interrupt_action()
            if running is not None:
                running.cancel()
        errors = []
        for binding, running, page, keys in owned:
            if running is not None:
                results = await asyncio.gather(running, return_exceptions=True)
                errors.extend(result for result in results if isinstance(result, DriverAbort))
            async with binding.lock:
                for pointer in tuple(binding.driver._pointers.values()):
                    for button in tuple(pointer._held):
                        try:
                            await pointer.up(button)
                        except Exception as exc:
                            errors.append(exc)
                if page is not None and not page.is_closed():
                    for key in reversed(keys):
                        try:
                            await page.keyboard.up(key)
                        except Exception as exc:
                            if not page.is_closed():
                                errors.append(exc)
                binding.frame = None
        if errors:
            # A failed release may not be hidden by successful cancellation.
            # The failed cleanup remains pinned; resume cannot clear it.
            raise DriverAbort("Paused browser input could not be fully released; stop is required") from errors[0]
        if task_id in self._stops:
            return await asyncio.shield(self._stops[task_id])
        return self.store.get_task(task_id)

    async def resume(self, task_id: str, expected_revision: int):
        """Return REOBSERVE only. The parent must renew authority and observe.

        A subsequent current RUNNING observation rearms input. Old proposals,
        observations and approvals are never restored by this method.
        """
        suspension = self._suspensions.get(task_id)
        if suspension is None or suspension.paused_revision != expected_revision:
            raise AdmissionDenied("This gateway must finish suspending the current revision before resume")
        await asyncio.shield(suspension.cleanup)
        if task_id in self._stops:
            raise AdmissionDenied("A stopped executor cannot resume")
        resumed = self.store.request_resume(task_id, expected_revision)
        for binding in self._browsers.values():
            if binding.owner == task_id:
                binding.suspended = False
                binding.frame = None
                # needs_rearm deliberately stays true; no authority is revived.
        return resumed

    async def stop(self, task_id: str, expected_revision: int):
        """Revoke first; drain/close owned input before publishing CANCELLED."""
        if task_id not in self._stops:
            try:
                self.store.request_stop(task_id, expected_revision)
            except Conflict:
                current = self.store.get_task(task_id)
                suspension = self._suspensions.get(task_id)
                # Only a pause caused by this exact competing request can
                # advance Stop's expected revision. Other stale CAS stays stale.
                if (suspension is None or suspension.requested_revision != expected_revision
                        or suspension.paused_revision != current.revision or current.status != "PAUSED"):
                    raise
                self.store.request_stop(task_id, current.revision)
            self._stops[task_id] = asyncio.create_task(self._stop_owned(task_id))
        # Cancelling a caller must not cancel cleanup or release held input early.
        return await asyncio.shield(self._stops[task_id])

    async def _stop_owned(self, task_id):
        owned = []
        for binding in self._browsers.values():
            if task_id == binding.owner:
                binding.closing = True
                binding.driver.interrupt_action()
                if binding.running is not None:
                    binding.running.cancel()
                owned.append(binding)
        for binding in owned:
            if binding.running is not None:
                await asyncio.gather(binding.running, return_exceptions=True)
            async with binding.lock:
                await binding.driver.close()
                binding.frame = None
        current = self.store.get_task(task_id)
        if current.status == "CANCELLING":
            current = self.store.transition(task_id, current.revision, "CANCELLED")
        return current
