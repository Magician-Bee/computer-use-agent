import json
import asyncio

import pytest

from benchmarks.cases import CASES, grade


def snapshot(key, value, **kwargs):
    return {"storage": {"origins": [{"localStorage": [{"name": key, "value": json.dumps(value)}]}]}, **kwargs}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_empty_or_done_only_state_does_not_pass(case):
    assert not grade(case.id, {})["passed"]
    assert not grade(case.id, {"status": "completed", "text": "done"})["passed"]


def test_profile_requires_every_field_and_submit():
    correct = {"name": "林沛安", "email": "pei.an@example.test", "department": "design", "seat": "window", "snack": True, "submitted": True}
    assert grade("profile", snapshot("bench_profile", correct))["passed"]
    for key in correct:
        wrong = correct | {key: None}
        assert not grade("profile", snapshot("bench_profile", wrong))["passed"]


def test_inventory_rejects_unrelated_edits_and_cleared_filter():
    state = {"stocks": {"A-101": 18, "B-204": 27, "C-310": 5, "D-402": 8}, "saved": ["B-204"]}
    result = snapshot("bench_inventory", state)
    storage = result["storage"]["origins"][0]["localStorage"]
    storage.extend([{"name": "bench_inventory_filter", "value": json.dumps("鍵盤")}, {"name": "bench_inventory_visible", "value": json.dumps(["B-204"])}])
    assert grade("inventory", result)["passed"]
    state["stocks"]["A-101"] = 999
    storage[0]["value"] = json.dumps(state)
    assert not grade("inventory", result)["passed"]
    state["stocks"]["A-101"] = 18
    storage[0]["value"] = json.dumps(state)
    storage[1]["value"] = json.dumps("")
    assert not grade("inventory", result)["passed"]


def test_supplier_requires_actual_retrieved_values_and_both_tabs():
    state = {"supplier": "星河工作室", "code": "GH-742", "email": "supply@starlake.example.test", "quantity": 24, "submitted": True}
    result = snapshot("bench_order", state, pages=[{"path": "/order.html"}, {"path": "/directory.html"}])
    assert grade("supplier", result)["passed"]
    result["pages"].pop()
    assert not grade("supplier", result)["passed"]


def test_local_fixture_capture_uses_real_browser_without_model():
    pytest.importorskip("playwright.async_api")
    from scripts.benchmark_models import CapturingBrowserDriver, fixture_server

    async def inspect(origin):
        captured = {}
        driver = CapturingBrowserDriver(start_url=origin + "/inventory.html", origin=origin, no_dom=False, result=captured)
        try:
            await driver.start()
        except RuntimeError as exc:
            if "Chromium" in str(exc):
                pytest.skip(str(exc))
            raise
        try:
            observation = await driver.observe()
            assert "機械鍵盤" in observation["text"]
            assert any(element.get("value") == "12" for element in observation["elements"])
        finally:
            await driver.close()
        assert captured["pages"][0]["path"] == "/inventory.html"
        result = grade("inventory", captured)
        assert result["observed_state"]["stocks"]["B-204"] == 12
        assert result["passed"] is False

    with fixture_server() as origin:
        asyncio.run(inspect(origin))
