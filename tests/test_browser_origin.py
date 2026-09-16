"""Headless HTTP/WS scope checks against two owned ephemeral loopback servers.

These tests do not certify WebRTC, DNS, browser background services or an OS
network sandbox. No test connects to an external host.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest
from playwright.async_api import Error as PlaywrightError

from server.core.contracts import ActionEnvelope, ResourceScope, SuccessCriterion, Task
from server.core.gateway import ActionGateway, browser_manifest
from server.core.policy import ExecutionPolicy, PolicyRegistry
from server.core.store import AdmissionDenied, TaskStore
from server.drivers import BrowserDriver
from server.schemas import Action


class RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.requests = []
        self.connections = 0
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.other_origin = None

    def get_request(self):
        connection = super().get_request()
        self.connections += 1
        return connection


@pytest.fixture
def servers():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.do_GET()

        def do_GET(self):
            self.server.requests.append((self.command, self.path))
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            destination = self.server.other_origin
            if destination and self.path in {"/redirect", "/post-redirect", "/safe-redirect"}:
                self.send_response(307 if self.path == "/post-redirect" else 302)
                self.send_header("Location", "/ok" if self.path == "/safe-redirect" else destination + "/redirect-target")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = b"<body>Same-origin response</body>"
            if self.path == "/slow":
                time.sleep(.3)
            if self.path == "/oversize":
                body = b"x" * (8 * 1024 * 1024 + 1)
            if destination and self.path == "/probes":
                body = ("""<body>
<iframe title="Blocked frame" src="OTHER/frame"></iframe>
<iframe title="Allowed frame" src="/ok"></iframe>
<iframe name="form-result" title="Form result"></iframe>
<form action="OTHER/form" method="post" target="form-result">
  <input name="value" value="fixture-only"><button>Submit outside</button>
</form>
<script>
const outside=OUTSIDE;
const wsProbe=url=>new Promise(resolve=>{
  const ws=new WebSocket(url);
  const timer=setTimeout(()=>{ws.close();resolve('timeout')},3000);
  ws.onclose=e=>{clearTimeout(timer);resolve(e.code)};
});
window.finished=false;
Promise.all([
  fetch(outside+'/post',{method:'POST',body:'fixture-only'}).then(()=> 'sent',()=> 'blocked'),
  fetch('/redirect').then(()=> 'sent',()=> 'blocked'),
  fetch('/post-redirect',{method:'POST',body:'fixture-only'}).then(()=> 'sent',()=> 'blocked'),
  wsProbe(outside.replace('http:','ws:')+'/ws'),
  wsProbe(location.origin.replace('http:','ws:')+'/ws')
]).then(results=>{window.results=results;window.finished=true});
</script>""").replace("OTHER", destination).replace("OUTSIDE", json.dumps(destination)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The timeout test deliberately closes its request early.

    outside = RecordingServer(Handler)
    allowed = RecordingServer(Handler)
    allowed.other_origin = outside.origin
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (allowed, outside)]
    for thread in threads:
        thread.start()
    try:
        yield allowed, outside
    finally:
        for server in (allowed, outside):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_origin_normalization_and_immutable_prestart_configuration():
    driver = BrowserDriver("https://EXAMPLE.test:443/path")
    driver.configure_allowed_origin("https://example.test:443/")
    assert driver.allowed_origin == "https://example.test"
    for replacement in (None, "https://example.test", "https://other.test"):
        with pytest.raises(RuntimeError):
            driver.configure_allowed_origin(replacement)
    with pytest.raises(AttributeError):
        driver.allowed_origin = "https://other.test"
    international = BrowserDriver("https://xn--bcher-kva.test/path")
    international.configure_allowed_origin("https://bücher.test")
    assert international.allowed_origin == "https://xn--bcher-kva.test"


@pytest.mark.parametrize("origin", [
    "https://other.test", "https://example.test/path", "https://example.test?query=1",
    "https://example.test#fragment", "https://user@example.test", "https://example.test\\@other.test",
    "https://example.test\n", "about:blank",
])
def test_ambiguous_non_origin_and_out_of_scope_start_are_rejected(origin):
    driver = BrowserDriver("https://example.test/page")
    with pytest.raises(ValueError):
        driver.configure_allowed_origin(origin)
    assert driver.allowed_origin is None


async def test_configuration_cannot_be_added_after_start_or_reopen(servers):
    allowed, _ = servers
    driver = BrowserDriver(allowed.origin)
    await driver.start()
    try:
        with pytest.raises(RuntimeError):
            driver.configure_allowed_origin(allowed.origin)
    finally:
        await driver.close()
    with pytest.raises(RuntimeError):
        driver.configure_allowed_origin(allowed.origin)


async def test_changed_start_url_is_rejected_before_browser_or_server_contact(servers):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin)
    driver.configure_allowed_origin(allowed.origin)
    driver.start_url = outside.origin
    with pytest.raises(ValueError):
        await driver.start()
    assert driver._playwright is None and driver._context is None
    assert allowed.connections == outside.connections == 0


async def test_cross_origin_frames_post_redirects_and_all_websockets_never_contact_target(servers):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin + "/probes")
    driver.configure_allowed_origin(allowed.origin)
    try:
        await driver.start()
        page = driver._page
        await page.wait_for_function("window.finished===true")
        assert await page.evaluate("results") == ["blocked", "blocked", "blocked", 1008, 1008]
        observation = await driver.observe()
        assert "Same-origin response" in observation["text"]
        assert observation["origin_scope_mode"] == "http_no_redirects"
        assert observation["allowed_origin"] == allowed.origin
        assert observation["redirects_supported"] is observation["websocket"] is observation["streaming"] is False
        submit = next(e["id"] for e in observation["elements"] if e["text"] == "Submit outside")
        async with page.expect_event("requestfailed", predicate=lambda request: request.url == outside.origin + "/form"):
            await driver.execute(Action(type="click", target=submit))
        assert ("GET", "/ok") in allowed.requests
        assert ("GET", "/redirect") in allowed.requests
        assert ("POST", "/post-redirect") in allowed.requests
        assert not any(path == "/ws" for _, path in allowed.requests)
    finally:
        await driver.close()
    assert outside.requests == []
    assert outside.connections == 0


async def test_all_redirects_and_cross_origin_navigation_are_rejected(servers):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin)
    driver.configure_allowed_origin(allowed.origin)
    try:
        await driver.start()
        await driver.execute(Action(type="navigate", url=allowed.origin + "/ok"))
        assert driver._page.url == allowed.origin + "/ok"
        with pytest.raises(PlaywrightError):
            await driver.execute(Action(type="navigate", url=allowed.origin + "/safe-redirect"))
        with pytest.raises(PlaywrightError):
            await driver.execute(Action(type="navigate", url=allowed.origin + "/redirect"))
        with pytest.raises(PlaywrightError):
            await driver.execute(Action(type="navigate", url=outside.origin + "/direct"))
    finally:
        await driver.close()
    assert outside.requests == []
    assert outside.connections == 0


async def test_oversized_response_is_not_forwarded_to_chromium(servers):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin)
    driver.configure_allowed_origin(allowed.origin)
    try:
        await driver.start()
        result = await driver._page.evaluate("fetch('/oversize').then(()=> 'received',()=> 'blocked')")
        assert result == "blocked"
        assert allowed.requests.count(("GET", "/oversize")) == 1
    finally:
        await driver.close()
    assert outside.requests == []


async def test_unscoped_browser_retains_existing_redirect_behavior(servers):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin)
    try:
        await driver.start()
        await driver.execute(Action(type="navigate", url=allowed.origin + "/redirect"))
        assert driver._page.url == outside.origin + "/redirect-target"
        observation = await driver.observe()
        assert "origin_scope_mode" not in observation
        assert ("GET", "/redirect-target") in outside.requests
    finally:
        await driver.close()


async def test_scoped_timeout_aborts_without_retry_or_browser_fallback(servers, monkeypatch):
    allowed, outside = servers
    driver = BrowserDriver(allowed.origin)
    driver.configure_allowed_origin(allowed.origin)
    try:
        await driver.start()
        # Exercise the real network timeout with a smaller test-only deadline.
        monkeypatch.setattr(driver, "_SCOPED_REQUEST_TIMEOUT_MS", 100)
        result = await driver._page.evaluate("fetch('/slow').then(()=> 'received',()=> 'blocked')")
        assert result == "blocked"
        assert allowed.requests.count(("GET", "/slow")) == 1
    finally:
        await driver.close()
    assert outside.requests == []


def scoped_gateway(store, origin):
    task = store.create_task(Task(id="origin-task", goal="Open a same-origin fixture tab",
        policy_ref="origin-policy", success_criteria=(
            SuccessCriterion(id="tab-opened", description="New tab contains the fixture response"),)))
    for status in ("QUEUED", "PLANNING", "READY", "RUNNING"):
        task = store.transition(task.id, task.revision, status)
    scope = ResourceScope(resource_id="origin-browser", kind="browser",
                          session_id="origin-session", website_origin=origin)
    policies = PolicyRegistry()
    policies.register(ExecutionPolicy(policy_id=task.policy_ref, resource_scopes=(scope,),
        allowed_action_types=("new_tab",), approval_action_types=()))
    return ActionGateway(store, policies), task, scope


async def test_gateway_rejects_origin_scope_that_was_not_configured_before_start(servers, tmp_path):
    allowed, outside = servers
    store = TaskStore(tmp_path / "unconfigured.sqlite3")
    gateway, task, scope = scoped_gateway(store, allowed.origin)
    driver = BrowserDriver(allowed.origin)
    try:
        await driver.start()
        assert driver._page.url == allowed.origin + "/"
        assert driver.allowed_origin is None
        with pytest.raises(AdmissionDenied, match="before startup"):
            gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("new_tab",)))
        assert driver._action_guard is None
        assert gateway._browsers == {}
    finally:
        await driver.close()
        store.close()
    assert outside.requests == []
    assert outside.connections == 0


async def test_gateway_authorized_new_tab_crosses_blank_intermediate_only_within_dispatch(servers, tmp_path):
    allowed, outside = servers
    store = TaskStore(tmp_path / "scoped-new-tab.sqlite3")
    gateway, task, scope = scoped_gateway(store, allowed.origin)
    driver = BrowserDriver(allowed.origin)
    driver.configure_allowed_origin(allowed.origin)
    try:
        await driver.start()
        original_page = driver._page
        gateway.register_browser(task.id, scope, driver, browser_manifest(scope, ("new_tab",)))
        lease = store.acquire_lease(task.id, scope, ttl_seconds=120)
        frame = await gateway.observe(task.id, task.revision, lease)
        envelope = ActionEnvelope.create(action_id="open-tab", task_id=task.id,
            task_revision=task.revision, step_id="open-tab-step", tool_id="browser.input",
            tool_version="1.0", action=Action(type="new_tab", url=allowed.origin + "/ok"),
            observation_id=frame.reference.id, observation_revision=frame.reference.revision,
            policy_ref=task.policy_ref, resource_scope=scope, approval_id=None,
            lease_id=lease.id, fencing_token=lease.fencing_token,
            idempotency_key="open-tab-once", created_at=store.clock())
        receipt = await gateway.dispatch(envelope)
        assert receipt["dispatch"] is True and receipt["state"] == "succeeded"
        assert receipt["outcome"]["task_completion_verified"] is False
        assert len(driver._context.pages) == 2 and driver._page is not original_page
        assert driver._page.url == allowed.origin + "/ok"
        refreshed = await gateway.observe(task.id, task.revision, lease)
        assert refreshed.reference.resource_scope == scope
        assert refreshed.reference.revision > frame.reference.revision
        assert refreshed.snapshot["url"] == allowed.origin + "/ok"
        assert refreshed.snapshot["origin_scope_mode"] == "http_no_redirects"
        assert "Same-origin response" in refreshed.snapshot["text"]
        assert store.get_task(task.id).status == "RUNNING"

        # A later blank document is not authorized by the completed new_tab.
        # Create it through trusted fixture setup, then exercise the real guard.
        same_origin_page = driver._page
        blank = await driver._context.new_page()
        driver._page = blank
        try:
            with pytest.raises(AdmissionDenied):
                await gateway.observe(task.id, task.revision, lease)
            with pytest.raises(AdmissionDenied, match="No active gateway dispatch"):
                await driver.execute(Action(type="key", key="Enter"))
        finally:
            driver._page = same_origin_page
            await blank.close()
    finally:
        await driver.close()
        store.close()
    assert outside.requests == []
    assert outside.connections == 0
