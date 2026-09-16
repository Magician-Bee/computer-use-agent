"""Regression checks for concrete cancellation, stale-state and boundary bugs.

All drivers/model transports are in-memory fakes; no desktop input or network.
"""

import asyncio
import copy
from types import SimpleNamespace

import httpx
import pytest

from server import agent, app as app_module, providers
from server.schemas import Action, ModelConfig, RunRequest


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


class FrameDriver:
    def __init__(self, **_kwargs):
        self.frame = "original button"
        self.executed = []
        self.observed = []
        self.closed = False

    async def start(self):
        pass

    async def observe(self):
        self.observed.append(self.frame)
        return {"width": 100, "height": 100, "title": "fixture", "text": self.frame, "elements": [{"id": "ocr_1", "source": "apple_vision", "role": "text", "text": self.frame, "x": 30, "y": 30, "width": 30, "height": 15}]}

    def set_observation(self, observation):
        self.latest = copy.deepcopy(observation)

    async def execute(self, action):
        self.executed.append((action, self.frame))
        return "performed"

    async def close(self):
        self.closed = True


def new_run(monkeypatch, driver, approval_mode="auto", max_steps=1):
    monkeypatch.setattr(agent, "BrowserDriver", lambda **kwargs: driver)
    return agent.Run(RunRequest(task="Click the original button", approval_mode=approval_mode, max_steps=max_steps), ModelConfig(provider="ollama"), "http://localhost/demo")


async def test_concurrent_stop_requests_cannot_interrupt_driver_cleanup(monkeypatch):
    planning = asyncio.Event()
    close_started = asyncio.Event()
    close_released = asyncio.Event()
    close_cancelled = asyncio.Event()

    class SlowCloseDriver(FrameDriver):
        async def close(self):
            close_started.set()
            try:
                await close_released.wait()
                self.closed = True
            except asyncio.CancelledError:
                close_cancelled.set()
                raise

    async def propose(*args):
        planning.set()
        await asyncio.Event().wait()

    driver = SlowCloseDriver()
    monkeypatch.setattr(agent, "next_action", propose)
    run = new_run(monkeypatch, driver)
    run.config.api_key = "ephemeral-regression-secret"
    run.task = asyncio.create_task(run.execute_loop())
    first = second = None
    try:
        await asyncio.wait_for(planning.wait(), 2)
        first = asyncio.create_task(run.stop())
        await asyncio.wait_for(close_started.wait(), 2)
        second = asyncio.create_task(run.stop())
        await asyncio.sleep(0.01)
        close_released.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert not close_cancelled.is_set(), "A second stop cancelled the cleanup of the first stop"
        assert driver.closed, "Run finished before driver resources were released"
        assert run.config.api_key == "", "Cancellation interrupted credential cleanup"
    finally:
        close_released.set()
        await asyncio.gather(*(task for task in (first, second, run.task) if task), return_exceptions=True)


async def test_pause_during_observation_forces_new_frame_before_planning(monkeypatch):
    observing = asyncio.Event()
    release_observe = asyncio.Event()
    planned = []

    class SlowObserveDriver(FrameDriver):
        async def observe(self):
            observation = await super().observe()
            if len(self.observed) == 1:
                observing.set()
                await release_observe.wait()
            return observation

    async def propose(config, task, observation, history):
        planned.append(observation["text"])
        return Action(type="done", text="No click needed")

    driver = SlowObserveDriver()
    monkeypatch.setattr(agent, "next_action", propose)
    run = new_run(monkeypatch, driver)
    run.task = asyncio.create_task(run.execute_loop())
    try:
        await asyncio.wait_for(observing.wait(), 2)
        run.paused = True
        run.revision += 1
        run.resume.clear()
        driver.frame = "new button after manual intervention"
        release_observe.set()
        await eventually(lambda: run.status == "paused")
        run.paused = False
        run.resume.set()
        await asyncio.wait_for(run.task, 2)
        assert planned
        assert planned[0] == driver.frame, "The model planned from a frame captured before pause/manual edits"
    finally:
        release_observe.set()
        await run.stop()


async def test_changed_ocr_target_during_approval_cannot_receive_old_click(monkeypatch):
    calls = []

    async def propose(config, task, observation, history):
        calls.append(observation["text"])
        if observation["text"] == "original button":
            return Action(type="click", target="ocr_1", reason="Click original button")
        return Action(type="done", text="Original target no longer visible")

    driver = FrameDriver()
    monkeypatch.setattr(agent, "next_action", propose)
    run = new_run(monkeypatch, driver, approval_mode="always")
    run.task = asyncio.create_task(run.execute_loop())
    try:
        await eventually(lambda: run.pending_action is not None)
        # A real user or page can change the frame while the app awaits approval.
        driver.frame = "different destructive button at same coordinates"
        run.approved = True
        run.approval.set()
        await asyncio.wait_for(run.task, 2)
        assert driver.executed == [], "Approved stale OCR coordinates were executed against a changed target"
    finally:
        await run.stop()


@pytest.mark.parametrize("provider,payload", [
    ("ollama", {"message": None}),
    ("ollama", []),
    ("anthropic", {"content": [None]}),
    ("gemini", {"candidates": [None]}),
    ("openai", {"choices": [{"message": None}]}),
    ("custom", {"choices": [{"message": {"content": [None]}}]}),
])
async def test_malformed_provider_shapes_raise_safe_retryable_provider_error(monkeypatch, provider, payload):
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return httpx.Response(200, json=payload)

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    config = ModelConfig(provider=provider, base_url="http://127.0.0.1:11434/v1", api_key="regression-secret")
    with pytest.raises(providers.ProviderError) as error:
        await providers.complete(config, "Test only")
    assert "regression-secret" not in str(error.value)


@pytest.mark.parametrize("origin", ["http://127.0.0.1:wrong-port", "http://[bad", "http://localhost:99999"])
async def test_malformed_origin_is_rejected_without_internal_server_error(origin):
    transport = httpx.ASGITransport(app=app_module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/api/config", headers={"Origin": origin})
    assert response.status_code == 403


async def test_pause_interrupts_inflight_action_and_reobserves_partial_effect(monkeypatch):
    from server.drivers import ActionInterrupted
    started = asyncio.Event()
    interrupted = asyncio.Event()
    planned = []

    class InterruptibleDriver(FrameDriver):
        def interrupt_action(self):
            interrupted.set()

        async def resume_actions(self):
            interrupted.clear()

        async def execute(self, action):
            self.frame = "partial change requires a fresh observation"
            started.set()
            await interrupted.wait()
            raise ActionInterrupted("fixture interrupted")

    async def propose(config, task, observation, history):
        planned.append(observation["text"])
        if observation["text"] == "original button":
            return Action(type="click", target="ocr_1")
        return Action(type="done", text="Observed the changed fixture")

    driver = InterruptibleDriver()
    monkeypatch.setattr(agent, "next_action", propose)
    run = new_run(monkeypatch, driver, max_steps=3)
    monkeypatch.setattr(app_module, "runs", {run.id: run})
    run.task = asyncio.create_task(run.execute_loop())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await app_module.pause_run(run.id)
        await eventually(lambda: any("INTERRUPTED" in item["result"] for item in run.history))
        assert run.status == "paused"
        assert planned == ["original button"]
        await app_module.resume_run(run.id)
        await asyncio.wait_for(run.task, 2)
        assert run.error is None
        assert planned[1:] == [driver.frame, driver.frame]
    finally:
        await run.stop()


async def test_pause_while_driver_observes_does_not_fail_run(monkeypatch):
    from server.drivers import ActionInterrupted
    observing = asyncio.Event()
    interrupted = asyncio.Event()

    class InterruptibleObservationDriver(FrameDriver):
        blocked_once = False
        def interrupt_action(self):
            interrupted.set()
        async def resume_actions(self):
            interrupted.clear()
        async def observe(self):
            if not self.blocked_once:
                self.blocked_once = True
                observing.set()
                await interrupted.wait()
                raise ActionInterrupted("observation interrupted")
            return await super().observe()

    async def propose(*args):
        return Action(type="done", text="fixture only")
    driver = InterruptibleObservationDriver()
    monkeypatch.setattr(agent, "next_action", propose)
    run = new_run(monkeypatch, driver)
    monkeypatch.setattr(app_module, "runs", {run.id: run})
    run.task = asyncio.create_task(run.execute_loop())
    try:
        await asyncio.wait_for(observing.wait(), 2)
        await app_module.pause_run(run.id)
        await eventually(lambda: any("畫面擷取已中斷" in item["message"] for item in run.events))
        assert run.status == "paused" and run.error is None
        await app_module.resume_run(run.id)
        await asyncio.wait_for(run.task, 2)
        assert run.status == "completed" and run.error is None
    finally:
        await run.stop()


async def test_stop_during_resume_drain_cannot_restart_task(monkeypatch):
    from fastapi import HTTPException
    entered = asyncio.Event()
    release = asyncio.Event()
    class DrainingDriver(FrameDriver):
        async def resume_actions(self):
            entered.set()
            await release.wait()
        def interrupt_action(self):
            pass
    run = new_run(monkeypatch, DrainingDriver())
    run.driver = DrainingDriver()
    run.paused, run.status = True, "paused"
    monkeypatch.setattr(app_module, "runs", {run.id: run})
    resuming = asyncio.create_task(app_module.resume_run(run.id))
    await entered.wait()
    await run.stop()
    release.set()
    with pytest.raises(HTTPException) as error:
        await resuming
    assert error.value.status_code == 409
    assert run.status == "stopped" and run.stop_requested
