import asyncio
import base64
from io import BytesIO

from PIL import Image
import pytest

from server.agent import Run
from server.schemas import Action, ModelConfig, RunRequest


class FakeDriver:
    instances = []
    def __init__(self, **kwargs):
        self.executed = []
        self.closed = False
        self.count = 0
        FakeDriver.instances.append(self)
    async def start(self): pass
    async def observe(self):
        self.count += 1
        return {"width": 100, "height": 100, "title": "fixture", "text": "fresh state", "elements": []}
    def set_observation(self, obs): pass
    async def execute(self, action):
        self.executed.append(action)
        return "performed"
    async def close(self): self.closed = True


@pytest.fixture(autouse=True)
def driver(monkeypatch):
    FakeDriver.instances = []
    monkeypatch.setattr("server.agent.BrowserDriver", FakeDriver)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


async def test_approval_prevents_actions_and_stop_cancels_wait(monkeypatch):
    async def propose(*args): return Action(type="click", x=10, y=10)
    monkeypatch.setattr("server.agent.next_action", propose)
    run = Run(RunRequest(task="test"), ModelConfig(provider="ollama"), "http://localhost/demo")
    run.task = asyncio.create_task(run.execute_loop())
    await wait_until(lambda: run.status == "awaiting_approval")
    assert not FakeDriver.instances[0].executed
    await run.stop()
    assert run.status == "stopped"
    assert not FakeDriver.instances[0].executed
    assert FakeDriver.instances[0].closed


async def test_done_requires_fresh_second_observation(monkeypatch):
    seen = []
    async def propose(config, task, observation, history):
        seen.append(FakeDriver.instances[0].count)
        return Action(type="done", text="verified")
    monkeypatch.setattr("server.agent.next_action", propose)
    run = Run(RunRequest(task="test", approval_mode="auto"), ModelConfig(provider="ollama"), "http://localhost/demo")
    await run.execute_loop()
    assert run.status == "completed"
    assert seen == [1, 2]
    assert FakeDriver.instances[0].closed


async def test_failed_action_reaches_next_planning_turn(monkeypatch):
    async def execute(self, action): raise ValueError("element moved")
    monkeypatch.setattr(FakeDriver, "execute", execute)
    async def propose(config, task, observation, history):
        if history:
            assert "FAILED: element moved" in history[0]["result"]
            return Action(type="done", text="could not finish")
        return Action(type="click", x=10, y=10)
    monkeypatch.setattr("server.agent.next_action", propose)
    run = Run(RunRequest(task="test", approval_mode="auto"), ModelConfig(provider="ollama"), "http://localhost/demo")
    await run.execute_loop()
    assert run.step == 1
    assert any(event["kind"] == "error" for event in run.events)


async def test_cancellation_during_inference_prevents_late_action(monkeypatch):
    started = asyncio.Event()
    async def propose(*args):
        started.set()
        await asyncio.sleep(20)
        return Action(type="click", x=5, y=5)
    monkeypatch.setattr("server.agent.next_action", propose)
    run = Run(RunRequest(task="test", approval_mode="auto"), ModelConfig(provider="ollama", api_key="transient"), "http://localhost/demo")
    run.task = asyncio.create_task(run.execute_loop())
    await started.wait()
    await run.stop()
    assert not FakeDriver.instances[0].executed
    assert run.config.api_key == ""
    assert "transient" not in str(run.snapshot())


async def test_intervention_invalidates_approved_plan(monkeypatch):
    tasks = []
    async def propose(config, task, observation, history):
        tasks.append(task)
        return Action(type="done", text="changed task") if len(tasks) > 1 else Action(type="click", x=10, y=10)
    monkeypatch.setattr("server.agent.next_action", propose)
    run = Run(RunRequest(task="initial"), ModelConfig(provider="ollama"), "http://localhost/demo")
    run.task = asyncio.create_task(run.execute_loop())
    await wait_until(lambda: run.status == "awaiting_approval")
    run.interventions.append("Do the revised task")
    run.approval.set()
    await run.task
    assert not FakeDriver.instances[0].executed
    assert "Do the revised task" in tasks[-1]
