"""Real headless input revocation; no model calls or physical desktop input."""

import pytest

from server.drivers import ActionInterrupted, BrowserDriver, DriverAbort
from server.motion import MotionPolicy
from server.schemas import Action


class AuthorityRevoked(RuntimeError):
    pass


class Authority:
    def __init__(self):
        self.revoked = False
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.revoked:
            raise AuthorityRevoked("lease revoked")


HTML = """<style>
input,select,button{display:block;margin:30px;width:180px;height:40px}
</style>
<input id="name" aria-label="Name" value="original">
<select id="choice" aria-label="Choice"><option>A</option><option>B</option></select>
<input id="file" type="file" aria-label="File">
<input id="hidden-file" type="file" style="display:none">
<button id="upload" onclick="document.getElementById('hidden-file').click()">Upload</button>
<button id="save" onclick="window.saved++">Save</button>
<script>window.saved=0;window.events=[];
for(const kind of ['pointermove','pointerdown','pointerup','click','input','change'])
document.addEventListener(kind,e=>events.push({kind,target:e.target.id,buttons:e.buttons}),true);
</script>"""


@pytest.fixture
async def browser(tmp_path):
    driver = BrowserDriver()
    driver.configure_motion(MotionPolicy(min_duration=.2, max_duration=.3))
    uploaded = tmp_path / "allowed.txt"
    uploaded.write_text("explicitly authorized")
    driver.configure_files([str(uploaded)], tmp_path / "downloads")
    await driver.start()
    try:
        await driver._page.set_content(HTML)
        await driver.observe()
        yield driver
    finally:
        await driver.close()


def target(driver, label):
    return next(e["id"] for e in driver._observation["elements"] if e["text"] == label)


def test_guard_binding_is_once_only_synchronous_and_cannot_be_removed():
    driver = BrowserDriver()
    async def async_check():
        pass
    for invalid in (None, 1, async_check):
        with pytest.raises(TypeError):
            driver.bind_action_guard(invalid)
    authority = Authority()
    driver.bind_action_guard(authority)
    for replacement in (None, authority, Authority()):
        with pytest.raises(RuntimeError):
            driver.bind_action_guard(replacement)
    driver.interrupt_action()
    with pytest.raises(ActionInterrupted):
        driver._action_boundary()
    assert authority.calls == 0
    driver._closing = True
    with pytest.raises(DriverAbort):
        driver._action_boundary()
    assert authority.calls == 0


def test_sync_wrapper_returning_coroutine_cannot_silently_authorize():
    driver = BrowserDriver()
    async def delayed_check():
        raise AuthorityRevoked()
    driver.bind_action_guard(lambda: delayed_check())
    with pytest.raises(TypeError, match="awaitable"):
        driver._action_boundary()


async def test_denied_guard_allows_start_observation_screenshot_and_cleanup():
    authority = Authority()
    authority.revoked = True
    driver = BrowserDriver()
    driver.bind_action_guard(authority)
    await driver.start()
    try:
        await driver._page.set_content(HTML)
        observation = await driver.observe()
        assert observation["image"].startswith("data:image/png;base64,")
        assert await driver._page.screenshot(type="jpeg")
        assert authority.calls == 0
        with pytest.raises(AuthorityRevoked):
            await driver.execute(Action(type="type", target=target(driver, "Name"), text="unauthorized"))
        assert await driver._page.locator("#name").input_value() == "original"
        calls = authority.calls
        await driver.observe()
        await driver.close()
        assert authority.calls == calls
        with pytest.raises(RuntimeError):
            driver.bind_action_guard(Authority())
    finally:
        await driver.close()


@pytest.mark.parametrize("kind,label,read_method", [
    ("type", "Name", "get_attribute"),
    ("select_option", "Choice", "evaluate"),
    ("upload_file", "File", "evaluate"),
])
async def test_revoked_during_actual_element_read_does_not_send_input(browser, monkeypatch, kind, label, read_method):
    authority = Authority()
    browser.bind_action_guard(authority)
    element_id = target(browser, label)
    handle = browser._handles[element_id]
    original = getattr(handle, read_method)

    async def read_then_revoke(*args, **kwargs):
        result = await original(*args, **kwargs)
        authority.revoked = True
        return result

    monkeypatch.setattr(handle, read_method, read_then_revoke)
    text = next(iter(browser._allowed_uploads)).as_posix() if kind == "upload_file" else "B"
    with pytest.raises(AuthorityRevoked):
        await browser.execute(Action(type=kind, target=element_id, text=text))
    assert await browser._page.locator("#name").input_value() == "original"
    assert await browser._page.locator("#choice").input_value() == "A"
    assert await browser._page.locator("#file").evaluate("el=>el.files.length") == 0
    assert await browser._page.evaluate("events.filter(e=>['input','change'].includes(e.kind))") == []


async def test_revoked_mid_motion_sends_no_mouse_down_or_click(browser, monkeypatch):
    authority = Authority()
    browser.bind_action_guard(authority)
    original = browser._page.mouse.move
    moves = 0

    async def move_then_revoke(*args, **kwargs):
        nonlocal moves
        await original(*args, **kwargs)
        moves += 1
        if moves == 4:
            authority.revoked = True

    monkeypatch.setattr(browser._page.mouse, "move", move_then_revoke)
    with pytest.raises(AuthorityRevoked):
        await browser.execute(Action(type="click", target=target(browser, "Save")))
    assert moves == 4
    events = await browser._page.evaluate("events")
    assert any(e["kind"] == "pointermove" for e in events)
    assert not any(e["kind"] in {"pointerdown", "click"} for e in events)
    assert await browser._page.evaluate("saved") == 0


async def test_revoked_after_mouse_down_still_releases_actual_button(browser, monkeypatch):
    authority = Authority()
    browser.bind_action_guard(authority)
    original = browser._page.mouse.down

    async def down_then_revoke(*args, **kwargs):
        await original(*args, **kwargs)
        authority.revoked = True

    monkeypatch.setattr(browser._page.mouse, "down", down_then_revoke)
    with pytest.raises(AuthorityRevoked):
        await browser.execute(Action(type="double_click", target=target(browser, "Save")))
    events = await browser._page.evaluate("events")
    assert sum(e["kind"] == "pointerdown" for e in events) == 1
    assert sum(e["kind"] == "pointerup" for e in events) == 1
    assert browser._pointer(browser._page).metadata()["pressed_buttons"] == []
    # Releasing an already delivered down can complete its click. The guard
    # cannot undo that first command, but prevents the second click's down.
    assert await browser._page.evaluate("saved") == 1


@pytest.mark.parametrize("kind", ["coordinate_type", "custom_upload"])
async def test_revoked_after_click_prevents_following_text_or_file_input(browser, monkeypatch, kind):
    authority = Authority()
    browser.bind_action_guard(authority)
    pointer = browser._pointer(browser._page)
    original = pointer.click

    async def click_then_revoke(*args, **kwargs):
        await original(*args, **kwargs)
        authority.revoked = True

    monkeypatch.setattr(pointer, "click", click_then_revoke)
    if kind == "coordinate_type":
        box = await browser._page.locator("#name").bounding_box()
        action = Action(type="type", x=box["x"] + 40, y=box["y"] + 20, text="unauthorized")
    else:
        action = Action(type="upload_file", target=target(browser, "Upload"),
                        text=next(iter(browser._allowed_uploads)).as_posix())
    with pytest.raises(AuthorityRevoked):
        await browser.execute(action)
    assert await browser._page.locator("#name").input_value() == "original"
    assert await browser._page.locator("#hidden-file").evaluate("el=>el.files.length") == 0
    assert await browser._page.evaluate("events.some(e=>e.kind==='click')") is True


async def test_revocation_after_new_page_prevents_navigation_without_undoing_page(browser, monkeypatch):
    authority = Authority()
    browser.bind_action_guard(authority)
    original = browser._context.new_page
    requested = []
    browser._context.on("request", lambda request: requested.append(request.url))
    await browser._context.route("https://fixture.invalid/**", lambda route: route.fulfill(body="unexpected navigation"))

    async def new_page_then_revoke(*args, **kwargs):
        page = await original(*args, **kwargs)
        authority.revoked = True
        return page

    monkeypatch.setattr(browser._context, "new_page", new_page_then_revoke)
    with pytest.raises(AuthorityRevoked):
        await browser.execute(Action(type="new_tab", url="https://fixture.invalid/forbidden"))
    assert len(browser._context.pages) == 2
    assert browser._page.url == "about:blank"
    assert requested == []
