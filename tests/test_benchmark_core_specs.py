"""Unchanged benchmark oracles exposed only to parent-side core verification.

These are unit/readback and real headless fixture tests, never model runs.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from benchmarks.cases import CASES, Case, grade
from benchmarks.core_specs import core_spec
from server.core.contracts import Task
from server.core.verifiers import VerifierRegistry
from server.drivers import BrowserDriver


PROFILE = {"name": "林沛安", "email": "pei.an@example.test", "department": "design",
           "seat": "window", "snack": True, "submitted": True}
INVENTORY = {"stocks": {"A-101": 18, "B-204": 27, "C-310": 5, "D-402": 8}, "saved": ["B-204"]}
SUPPLIER = {"supplier": "星河工作室", "code": "GH-742", "email": "supply@starlake.example.test",
            "quantity": 24, "submitted": True}
ORIGIN = "http://127.0.0.1:54321"


def state_snapshot(case_id):
    entries = {
        "profile": {"bench_profile": PROFILE},
        "inventory": {"bench_inventory": INVENTORY, "bench_inventory_filter": "鍵盤",
                      "bench_inventory_visible": ["B-204"]},
        "supplier": {"bench_order": SUPPLIER},
    }[case_id]
    return {"storage": {"origins": [{"origin": ORIGIN,
        "localStorage": [{"name": key, "value": json.dumps(value, ensure_ascii=False)} for key, value in entries.items()]}]},
        "pages": [{"url": ORIGIN + path, "path": path} for path in
                  (["/order.html", "/directory.html"] if case_id == "supplier" else [f"/{case_id}.html"])]}


def replace_record(snapshot, key, value):
    item = next(item for item in snapshot["storage"]["origins"][0]["localStorage"] if item["name"] == key)
    item["value"] = json.dumps(value, ensure_ascii=False)


class ReadbackContext:
    def __init__(self, snapshot):
        self.snapshot = deepcopy(snapshot)
        self.pages = [SimpleNamespace(url=page["url"], is_closed=lambda: False) for page in snapshot["pages"]]
        self.reads = 0

    async def storage_state(self):
        self.reads += 1
        return deepcopy(self.snapshot["storage"])


def driver_for(snapshot):
    return SimpleNamespace(_context=ReadbackContext(snapshot))


def original_oracle_cases():
    for case in CASES:
        yield pytest.param(case.id, state_snapshot(case.id), id=f"{case.id}-correct")
        if case.id in {"profile", "supplier"}:
            original = PROFILE if case.id == "profile" else SUPPLIER
            key = "bench_profile" if case.id == "profile" else "bench_order"
            for field in original:
                snapshot = state_snapshot(case.id)
                replace_record(snapshot, key, original | {field: None})
                yield pytest.param(case.id, snapshot, id=f"{case.id}-wrong-{field}")
        if case.id == "inventory":
            for sku in INVENTORY["stocks"]:
                snapshot = state_snapshot(case.id)
                wrong = deepcopy(INVENTORY)
                wrong["stocks"][sku] = 999
                replace_record(snapshot, "bench_inventory", wrong)
                yield pytest.param(case.id, snapshot, id=f"inventory-wrong-{sku}")
            for key, value in (("bench_inventory", INVENTORY | {"saved": ["A-101", "B-204"]}),
                               ("bench_inventory_filter", ""), ("bench_inventory_visible", ["A-101", "B-204"])):
                snapshot = state_snapshot(case.id)
                replace_record(snapshot, key, value)
                yield pytest.param(case.id, snapshot, id=f"inventory-wrong-{key}")
        if case.id == "supplier":
            for path in ("/order.html", "/directory.html"):
                snapshot = state_snapshot(case.id)
                snapshot["pages"] = [page for page in snapshot["pages"] if page["path"] != path]
                yield pytest.param(case.id, snapshot, id=f"supplier-missing-{path}")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
def test_spec_preserves_original_task_and_every_original_check(case):
    spec = core_spec(case)
    assert spec.case is case
    assert spec.case.task == case.task
    assert spec.browser_conditions == ()  # No extra DOM pass/fail conditions.
    assert spec.check_keys == tuple(grade(case.id, {})["checks"])
    assert len(spec.success_criteria) == len(spec.check_keys)
    assert len({item.id for item in spec.success_criteria}) == len(spec.check_keys)
    assert {item.verifier_ref for item in spec.success_criteria} == {spec.verifier_id}
    assert core_spec(case.id) == spec


def test_modified_or_unknown_case_cannot_change_the_original_task():
    with pytest.raises(ValueError, match="Unknown"):
        core_spec("nonexistent")
    original = CASES[0]
    with pytest.raises(ValueError, match="unchanged"):
        core_spec(Case(original.id, original.path, "Only say done", original.description))


@pytest.mark.parametrize("case_id,snapshot", list(original_oracle_cases()))
async def test_callback_matches_original_oracle_for_every_positive_and_negative_check(case_id, snapshot):
    spec = core_spec(case_id)
    driver = driver_for(snapshot)
    results = await spec.make_callback()(driver)
    original = grade(case_id, snapshot)
    assert tuple(item.criterion_id for item in results) == tuple(item.id for item in spec.success_criteria)
    assert [item.status for item in results] == ["pass" if value else "fail" for value in original["checks"].values()]
    assert all(item.status == "pass" for item in results) is original["passed"]
    assert driver._context.reads == 1


async def test_supplier_callback_and_criteria_do_not_return_hidden_answers_or_raw_storage():
    spec = core_spec("supplier")
    results = await spec.make_callback()(driver_for(state_snapshot("supplier")))
    outgoing = json.dumps({"task": spec.case.task,
        "criteria": [item.model_dump(mode="json") for item in spec.success_criteria],
        "results": [item.model_dump(mode="json") for item in results]}, ensure_ascii=False)
    for private in ("GH-742", "supply@starlake.example.test", "bench_order", "localStorage", "observed_state", "selector", "expected"):
        assert private not in outgoing
    assert all(item.reason == "oracle_pass" and item.condition_results == () for item in results)
    # This test bounds the adapter output. The runner must independently keep
    # parent specs/results out of the planner's prompt and observation payload.


@pytest.mark.parametrize("failure", ["closed", "capture-error", "malformed-record"])
async def test_unavailable_or_malformed_readback_is_inconclusive_without_exception_details(failure):
    snapshot = state_snapshot("profile")
    driver = driver_for(snapshot)
    if failure == "closed":
        driver._context = None
    elif failure == "capture-error":
        async def failed_capture():
            raise RuntimeError("CANARY-private-browser-content")
        driver._context.storage_state = failed_capture
    else:
        replace_record(driver._context.snapshot, "bench_profile", ["CANARY-private-browser-content"])
    results = await core_spec("profile").make_callback()(driver)
    assert all(item.status == "inconclusive" and item.reason == "oracle_inconclusive" for item in results)
    assert "CANARY" not in str(results)


async def test_readback_cancellation_propagates_instead_of_becoming_a_verdict():
    driver = driver_for(state_snapshot("profile"))
    async def cancelled_capture():
        raise asyncio.CancelledError
    driver._context.storage_state = cancelled_capture
    with pytest.raises(asyncio.CancelledError):
        await core_spec("profile").make_callback()(driver)


@pytest.mark.parametrize("case_id", [case.id for case in CASES])
async def test_original_headless_fixtures_require_real_commits_and_preserve_oracle_invariants(case_id):
    from scripts.benchmark_models import fixture_server

    spec = core_spec(case_id)
    task = Task(id=f"core-benchmark-{case_id}", goal=spec.case.task, policy_ref="fixture-parent-policy",
        success_criteria=spec.success_criteria)
    registry = VerifierRegistry()
    registered = registry.register_callback(task, spec.make_callback(), verifier_id=spec.verifier_id)
    assert registered.mode == "callback" and registered.conditions == ()

    async def callback(driver):
        return await registry.verify(task.id, driver)

    with fixture_server() as origin:
        driver = BrowserDriver(headless=True, start_url=origin + spec.case.path)
        driver.configure_allowed_origin(origin)
        try:
            await driver.start()
            page = driver._page
            assert not all(item.status == "pass" for item in await callback(driver))
            if case_id == "profile":
                await page.locator('[name="name"]').fill(PROFILE["name"])
                await page.locator('[name="email"]').fill(PROFILE["email"])
                await page.locator('[name="department"]').select_option(PROFILE["department"])
                await page.locator('[name="seat"]').select_option(PROFILE["seat"])
                await page.locator('[name="snack"]').check()
                await page.locator('#status').evaluate("node => node.textContent='報名已送出'")
                assert not all(item.status == "pass" for item in await callback(driver))
                await page.get_by_role("button", name="送出報名").click()
            elif case_id == "inventory":
                await page.locator('#search').fill("鍵盤")
                await page.get_by_role("spinbutton", name="機械鍵盤庫存").fill("27")
                await page.locator('#status').evaluate("node => node.textContent='已儲存'")
                assert not all(item.status == "pass" for item in await callback(driver))
                await page.get_by_role("button", name="儲存機械鍵盤", exact=True).click()
            else:
                async with driver._context.expect_page() as popup:
                    await page.get_by_role("link", name="開啟供應商目錄（新分頁）").click()
                directory = await popup.value
                await directory.wait_for_load_state("domcontentloaded")
                row = directory.locator("tbody tr").filter(has_text="星河工作室")
                retrieved = await row.locator("td").all_text_contents()
                assert retrieved == [SUPPLIER["supplier"], SUPPLIER["code"], SUPPLIER["email"]]
                for field, value in {"supplier": retrieved[0], "code": retrieved[1], "email": retrieved[2], "quantity": "24"}.items():
                    await page.locator(f'[name="{field}"]').fill(value)
                await page.locator('#status').evaluate("node => node.textContent='申請已送出'")
                assert not all(item.status == "pass" for item in await callback(driver))
                await page.get_by_role("button", name="送出申請").click()
            assert all(item.status == "pass" for item in await callback(driver))

            if case_id == "profile":
                # The original oracle grades the committed record. Current
                # form values are not an additional or replacement criterion.
                await page.locator('[name="name"]').fill("unsaved change")
                assert all(item.status == "pass" for item in await callback(driver))
            elif case_id == "inventory":
                await page.locator('#search').fill("")
                await page.get_by_role("spinbutton", name="USB-C 傳輸線庫存").fill("999")
                await page.get_by_role("button", name="儲存USB-C 傳輸線", exact=True).click()
                await page.locator('#search').fill("鍵盤")
                assert await page.get_by_role("spinbutton", name="機械鍵盤庫存").input_value() == "27"
                results = await callback(driver)
                statuses = dict(zip(spec.check_keys, [item.status for item in results], strict=True))
                assert statuses["stock_B-204"] == "pass"
                assert statuses["stock_A-101"] == statuses["saved_target"] == "fail"
            else:
                await directory.close()
                await driver.observe()  # Reobserve the remaining active page.
                results = await callback(driver)
                statuses = dict(zip(spec.check_keys, [item.status for item in results], strict=True))
                assert statuses["submitted"] == "pass" and statuses["directory_tab_open"] == "fail"
        finally:
            await driver.close()
