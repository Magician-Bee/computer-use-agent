"""Deterministic candidate verifier tests; real headless DOM, no models/input."""
import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from server.core.contracts import SuccessCriterion, Task
from server.core.verifiers import BrowserCondition, CheckResult, VerifierRegistry
from server.drivers import BrowserDriver


def task(*criteria):
    return Task(id="verify-task", goal="Read the parent-specified browser result", policy_ref="policy",
                success_criteria=tuple(SuccessCriterion(id=value, description="Parent-owned check") for value in criteria))


def condition(identifier="name-value", criterion="profile", kind="value", selector="#name", value="Ada"):
    return BrowserCondition(id=identifier, criterion_id=criterion, kind=kind, selector=selector, value=value)


@pytest.fixture
async def browser():
    driver = BrowserDriver()
    await driver.start()
    try:
        await driver._page.set_content("""<input id="name" value="Ada">
<input id="agreed" type="checkbox" checked><input id="unagreed" type="checkbox">
<select id="colors" multiple><option value="red" selected>Red</option><option value="blue" selected>Blue</option></select>
<p id="status">Saved exactly</p><p id="empty"></p>
<div style="display:none"><p id="hidden">Hidden</p></div>
<div class="duplicate">One</div><div class="duplicate">Two</div>
<div id="credentials"><input id="password" type="password" value="NEVER-RETURN-PASSWORD"></div>
<div data-secret><p id="secret">NEVER-RETURN-SECRET</p></div>
<script>window.inputEvents=[];
for(const k of ['focusin','keydown','pointerdown','click','input','change'])
document.addEventListener(k,e=>inputEvents.push(k));</script>""")
        yield driver
    finally:
        await driver.close()


@pytest.mark.parametrize("selector", ["xpath=//input", "text=Ada", "#name >> nth=0",
    "#name, #status", "input:has-text('Ada')", "input:nth-child(1)", "#name\\31", "[onclick='x']"])
def test_parent_selector_subset_rejects_engines_composition_and_unbounded_syntax(selector):
    with pytest.raises(ValidationError):
        condition(selector=selector)


def test_condition_types_and_empty_exact_values_are_explicit():
    assert condition(value="").value == ""
    assert condition(selector='main > input[data-testid="user-name"]')
    with pytest.raises(ValidationError):
        condition(kind="checked", value="true")
    with pytest.raises(ValidationError):
        condition(kind="url_same_origin", selector=None, value="https://fixture.test/path")
    with pytest.raises(ValidationError):
        condition(kind="url_exact", selector=None, value="https://user:secret@fixture.test")


def test_registry_requires_complete_unique_criteria_and_pins_immutable_specs():
    registry = VerifierRegistry()
    criteria = task("profile", "saved")
    profile = condition()
    saved = condition("saved-text", "saved", "text", "#status", "Saved exactly")
    with pytest.raises(ValidationError):
        registry.register(criteria, (profile,))
    with pytest.raises(ValidationError):
        registry.register(criteria, (profile, saved, condition("unknown", "unknown")))
    with pytest.raises(ValidationError):
        registry.register(criteria, (profile, profile, saved))
    spec = registry.register(criteria, (saved, profile))
    assert registry.register(criteria, (profile, saved)) == spec
    with pytest.raises(ValueError, match="immutable"):
        registry.register(criteria, (condition(value="Mallory"), saved))
    assert registry.spec_for(criteria.id) == spec
    with pytest.raises(LookupError):
        registry.spec_for("not-registered")


async def test_exact_readback_aggregates_all_conditions_from_fresh_dom_without_input(browser):
    registry = VerifierRegistry()
    checks = (
        condition(), condition("agreed", "profile", "checked", "#agreed", True),
        condition("not-agreed", "profile", "checked", "#unagreed", False),
        condition("colors", "profile", "selected_values", "#colors", ("red", "blue")),
        condition("saved", "status", "text", "#status", "Saved exactly"),
        condition("empty", "status", "text", "#empty", ""),
        condition("hidden", "status", "visible", "#hidden", False),
        condition("shown", "status", "visible", "#status", True),
        condition("gone", "status", "exists", "#deleted", False),
    )
    registry.register(task("profile", "status"), checks)
    original_observation = await browser.observe()
    original_focus = await browser._page.evaluate("document.activeElement.tagName")
    first = await registry.verify("verify-task", browser)
    assert [result.status for result in first] == ["pass", "pass"]
    assert all(result.condition_results for result in first)
    # Old observation still says Ada; verifier must read the current real field.
    await browser._page.locator("#name").evaluate("el=>el.value='Changed after observation'")
    second = await registry.verify("verify-task", browser)
    assert [result.status for result in second] == ["fail", "pass"]
    assert browser._observation is original_observation
    assert await browser._page.evaluate("document.activeElement.tagName") == original_focus
    assert await browser._page.evaluate("inputEvents") == []
    assert "Changed after observation" not in str(second)


async def test_absent_ambiguous_and_password_or_nested_secret_never_pass_or_leak(browser):
    registry = VerifierRegistry()
    registry.register(task("missing", "duplicate", "password", "nested", "secret"), (
        condition("missing", "missing", "text", "#absent", "anything"),
        condition("duplicate", "duplicate", "exists", ".duplicate", True),
        condition("password", "password", "value", "#password", "NEVER-RETURN-PASSWORD"),
        condition("nested", "nested", "text", "#credentials", ""),
        condition("secret", "secret", "text", "#secret", "NEVER-RETURN-SECRET"),
    ))
    results = await registry.verify("verify-task", browser)
    assert all(result.status == "inconclusive" for result in results)
    reasons = {result.condition_results[0].reason for result in results}
    assert reasons == {"missing", "ambiguous", "sensitive"}
    serialized = "\n".join(result.model_dump_json() for result in results)
    assert "NEVER-RETURN" not in serialized
    assert "NEVER-RETURN" not in repr(registry.spec_for("verify-task"))


async def test_url_conditions_compare_actual_page_not_a_planner_summary(browser):
    # Owned route fixture; this URL never makes a network connection.
    await browser._context.route("https://fixture.test/**", lambda route: route.fulfill(body="Saved"))
    await browser._page.goto("https://fixture.test/result")
    registry = VerifierRegistry()
    registry.register(task("location"), (
        condition("exact", "location", "url_exact", None, "https://fixture.test/result"),
        condition("origin", "location", "url_same_origin", None, "https://FIXTURE.test:443"),
    ))
    assert (await registry.verify("verify-task", browser))[0].status == "pass"
    await browser._page.goto("https://fixture.test/different")
    assert (await registry.verify("verify-task", browser))[0].status == "fail"


async def test_callback_registration_is_parent_only_immutable_and_restamps_readback(browser):
    registry = VerifierRegistry()
    stale = datetime(2020, 1, 1, tzinfo=timezone.utc)
    async def callback(driver):
        value = await driver._page.locator("#name").input_value()
        return (CheckResult(criterion_id="profile", status="pass" if value == "Ada" else "fail",
                            reason="oracle_pass", observed_at=stale),)
    spec = registry.register_callback(task("profile"), callback, verifier_id="original-oracle")
    assert registry.register_callback(task("profile"), callback, verifier_id="original-oracle") == spec
    with pytest.raises(TypeError):
        registry.register_callback(task("profile"), "model-supplied code", verifier_id="original-oracle")
    async def replacement(driver):
        return ()
    with pytest.raises(ValueError, match="immutable"):
        registry.register_callback(task("profile"), replacement, verifier_id="original-oracle")
    result = (await registry.verify("verify-task", browser))[0]
    assert result.status == "pass" and result.observed_at > stale
    assert await browser._page.evaluate("inputEvents") == []


@pytest.mark.parametrize("ids", [("profile",), ("profile", "profile"), ("profile", "unknown")])
async def test_callback_missing_duplicate_or_unknown_criteria_are_inconclusive(browser, ids):
    registry = VerifierRegistry()
    async def callback(driver):
        return tuple(CheckResult(criterion_id=value, status="pass", reason="oracle_pass") for value in ids)
    registry.register_callback(task("profile", "saved"), callback, verifier_id="original-oracle")
    results = await registry.verify("verify-task", browser)
    assert {result.criterion_id for result in results} == {"profile", "saved"}
    assert all(result.status == "inconclusive" and result.reason == "invalid_callback" for result in results)


async def test_callback_exception_and_deadline_do_not_return_raw_errors_or_pass(browser):
    failed = VerifierRegistry()
    async def exception(driver):
        raise ValueError("NEVER-RETURN-SECRET")
    failed.register_callback(task("profile"), exception, verifier_id="oracle")
    result = (await failed.verify("verify-task", browser))[0]
    assert result.status == "inconclusive" and result.reason == "read_failed"
    assert "NEVER-RETURN" not in result.model_dump_json()
    timed = VerifierRegistry(timeout_seconds=.1)
    async def slow(driver):
        await asyncio.Event().wait()
    timed.register_callback(task("profile"), slow, verifier_id="oracle")
    result = (await asyncio.wait_for(timed.verify("verify-task", browser), 1))[0]
    assert result.status == "inconclusive" and result.reason == "timeout"


async def test_document_replacement_during_callback_invalidates_its_pass(browser):
    registry = VerifierRegistry()
    began, resume = asyncio.Event(), asyncio.Event()
    async def callback(driver):
        began.set()
        await resume.wait()
        return (CheckResult(criterion_id="profile", status="pass", reason="oracle_pass"),)
    registry.register_callback(task("profile"), callback, verifier_id="oracle")
    reading = asyncio.create_task(registry.verify("verify-task", browser))
    await began.wait()
    # External fixture navigation while the readback is pending; no user input.
    await browser._page.goto("about:blank")
    resume.set()
    result = (await reading)[0]
    assert result.status == "inconclusive" and result.reason in {"page_changed", "read_failed"}


async def test_unavailable_closed_headed_or_busy_driver_cannot_pass(browser):
    registry = VerifierRegistry()
    registry.register(task("profile"), (condition(),))
    assert (await registry.verify("verify-task", object()))[0].reason == "unsupported_driver"
    assert (await registry.verify("verify-task", BrowserDriver(headless=False)))[0].reason == "unsupported_driver"
    browser._actions.add(asyncio.current_task())
    try:
        assert (await registry.verify("verify-task", browser))[0].reason == "busy_driver"
    finally:
        browser._actions.clear()
    await browser.close()
    assert (await registry.verify("verify-task", browser))[0].reason == "no_page"
