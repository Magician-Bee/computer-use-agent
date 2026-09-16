"""Actual headless input plus fake deterministic enrichment, no model or OS input."""
import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from server.core.contracts import ActionEnvelope, ResourceScope, SuccessCriterion, Task
from server.core.gateway import ActionGateway, browser_manifest
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import AdmissionDenied, TaskStore
from server.drivers import BrowserDriver
from server.schemas import Action


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@dataclass
class World:
    store: TaskStore
    gateway: ActionGateway
    driver: BrowserDriver
    task: Task
    scope: ResourceScope
    lease: object
    clock: Clock

    async def observe(self, **kwargs):
        return await self.gateway.observe(self.task.id, self.task.revision, self.lease, **kwargs)

    def envelope(self, frame, *, target="ocr_1"):
        return ActionEnvelope.create(action_id="save", task_id=self.task.id,
            task_revision=self.task.revision, step_id="save", tool_id="browser.input", tool_version="1.0",
            action=Action(type="click", target=target), observation_id=frame.reference.id,
            observation_revision=frame.reference.revision, policy_ref=self.task.policy_ref,
            resource_scope=self.scope, lease_id=self.lease.id, fencing_token=self.lease.fencing_token,
            idempotency_key="save-once", created_at=self.clock())


def as_ocr(snapshot):
    # Grounding transport test only: the known fixture box stands in for OCR.
    # This does not exercise or claim recognition accuracy.
    source = next(item for item in snapshot["elements"] if item["text"] == "Save fixture")
    snapshot["elements"] = [{**source, "id": "ocr_1", "source": "fixture_ocr", "role": "text"}]
    snapshot["text"] = "Recognized Save fixture"
    snapshot["perception"] = {"engine": "fake_for_transport_test"}
    return snapshot


@pytest.fixture
async def world(tmp_path):
    instances = []
    servers = []

    async def create(transform, *, scoped=False):
        clock = Clock()
        store = TaskStore(tmp_path / f"{len(instances)}.sqlite3", clock=clock)
        task = store.create_task(Task(id=f"task-{len(instances)}", goal="Click the disposable fixture button",
            policy_ref="policy", created_at=clock(), updated_at=clock(),
            success_criteria=(SuccessCriterion(id="clicked", description="Fixture saved once"),)))
        for state in ("QUEUED", "PLANNING", "READY", "RUNNING"):
            task = store.transition(task.id, task.revision, state)
        origin = None
        if scoped:
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b"<!doctype html><title>Disposable fixture</title>"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            servers.append((server, thread))
            origin = f"http://127.0.0.1:{server.server_port}"
        scope = ResourceScope(resource_id="browser", kind="browser", session_id="fixture", website_origin=origin)
        policies = PolicyRegistry()
        policies.register(ExecutionPolicy(policy_id="policy", resource_scopes=(scope,),
            allowed_action_types=("click",), approval_action_types=()))
        gateway = ActionGateway(store, policies, observation_transform=transform)
        driver = BrowserDriver(headless=True, start_url=origin or "about:blank")
        if scoped:
            driver.configure_allowed_origin(scope.website_origin)
        instance = World(store, gateway, driver, task, scope, None, clock)
        instances.append(instance)
        await driver.start()
        await driver._page.set_content('''<button id="save" style="margin:100px;width:140px;height:50px">Save fixture</button>
<script>window.saved=0;window.inputs=[];
for(const kind of ['pointerdown','click','keydown','input'])document.addEventListener(kind,e=>inputs.push(kind));
save.onclick=()=>saved++;</script>''')
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("click",)))
        instance.lease = store.acquire_lease(task.id, scope, ttl_seconds=120)
        return instance

    yield create
    for instance in instances:
        current = instance.store.get_task(instance.task.id)
        await asyncio.wait_for(instance.gateway.stop(current.id, current.revision), 5)
        instance.store.close()
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


async def test_full_and_quick_enrichment_grounds_actual_ocr_target_click(world):
    calls = []

    async def transform(snapshot, *, quick):
        calls.append(quick)
        return as_ocr(snapshot)

    instance = await world(transform)
    frame = await instance.observe()
    assert calls == [False]
    assert set(instance.driver._targets) == {"ocr_1"}
    assert "Save fixture" in frame.snapshot["_grounding_text"]
    image = base64.b64decode(frame.snapshot["image"].partition(",")[2])
    assert frame.reference.image_digest == hashlib.sha256(image).hexdigest()
    result = await instance.gateway.dispatch(instance.envelope(frame))
    assert calls == [False, True]
    assert result["state"] == "succeeded"
    assert await instance.driver._page.evaluate("saved") == 1
    assert await instance.driver._page.evaluate("inputs") == ["pointerdown", "click"]


@pytest.mark.parametrize("field,value", [("image", "fake pixels"), ("width", 640),
    ("physical_input_untouched", False), ("_grounding_text", "fake raw text"), ("invented_authority", True)])
async def test_transform_cannot_rewrite_or_add_executor_authority(world, field, value):
    async def transform(snapshot, *, quick):
        snapshot[field] = value
        return snapshot

    instance = await world(transform)
    with pytest.raises(AdmissionDenied, match="authority metadata"):
        await instance.observe()
    assert instance.store.latest_observation_revision(instance.task.id, instance.scope.resource_id) == 0
    assert await instance.driver._page.evaluate("inputs") == []
    assert instance.driver._observation["width"] == 1280


async def test_capture_timestamp_precedes_slow_enrichment_and_does_not_extend_ttl(world, monkeypatch):
    async def transform(snapshot, *, quick):
        instance.clock.now += timedelta(seconds=10)
        return as_ocr(snapshot)

    instance = await world(transform)
    original = instance.driver.observe

    async def delayed_capture():
        result = await original()
        instance.clock.now += timedelta(seconds=2)
        return result

    monkeypatch.setattr(instance.driver, "observe", delayed_capture)
    before = instance.clock()
    frame = await instance.observe()
    assert instance.clock() == before + timedelta(seconds=12)
    assert frame.reference.observed_at == before
    assert frame.reference.expires_at == before + timedelta(seconds=30)


async def test_already_expired_enrichment_is_not_published(world):
    async def transform(snapshot, *, quick):
        instance.clock.now += timedelta(seconds=31)
        return as_ocr(snapshot)

    instance = await world(transform)
    with pytest.raises(AdmissionDenied, match="expired"):
        await instance.observe()
    assert instance.store.latest_observation_revision(instance.task.id, instance.scope.resource_id) == 0
    assert "ocr_1" not in instance.driver._targets


async def test_quick_enrichment_cannot_revive_expired_action_observation(world):
    async def transform(snapshot, *, quick):
        if quick:
            instance.clock.now += timedelta(seconds=31)
        return as_ocr(snapshot)

    instance = await world(transform)
    frame = await instance.observe()
    with pytest.raises(AdmissionDenied, match="[Oo]bservation"):
        await instance.gateway.dispatch(instance.envelope(frame))
    assert await instance.driver._page.evaluate("inputs") == []
    assert not instance.store.pending_effects(instance.task.id)
    assert not any(event.type == "action.dispatched" for event in instance.store.events(instance.task.id))


@pytest.mark.parametrize("phase", ["observe", "preflight"])
async def test_stop_cancels_slow_transform_before_any_input_and_drains_owned_browser(world, phase):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def transform(snapshot, *, quick):
        if quick or phase == "observe":
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return as_ocr(snapshot)

    instance = await world(transform)
    if phase == "observe":
        running = asyncio.create_task(instance.observe())
    else:
        frame = await instance.observe()
        running = asyncio.create_task(instance.gateway.dispatch(instance.envelope(frame)))
    await asyncio.wait_for(entered.wait(), 3)
    assert await instance.driver._page.evaluate("inputs") == []
    stopped = await asyncio.wait_for(instance.gateway.stop(instance.task.id, instance.task.revision), 3)
    with pytest.raises(asyncio.CancelledError):
        await running
    assert cancelled.is_set()
    assert stopped.status == "CANCELLED"
    assert instance.driver._context is None
    assert not instance.store.pending_effects(instance.task.id)


@pytest.mark.parametrize("change", ["lease", "origin"])
async def test_authority_is_rechecked_after_enrichment(world, change):
    async def transform(snapshot, *, quick):
        if change == "lease":
            instance.store.release_lease(instance.lease.id, instance.lease.fencing_token)
        else:
            # Parent fixture changes the active page during the read. A model
            # has no access to this raw context method through the gateway.
            instance.driver._page = await instance.driver._context.new_page()
        return as_ocr(snapshot)

    instance = await world(transform, scoped=change == "origin")
    with pytest.raises(AdmissionDenied):
        await instance.observe()
    assert instance.store.latest_observation_revision(instance.task.id, instance.scope.resource_id) == 0
