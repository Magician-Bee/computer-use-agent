#!/usr/bin/env python3
"""Real production-workspace HTTP E2E with an owned server and headless browsers.

Runs only the fixed demo via UI buttons, then a separate demo stopped before its
first action. Uses diagnose_preview_http's model trap and read-only close oracle.
Never contacts port 8765, launches native input, or replaces production files.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def frontend_hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "frontend" / "dist").rglob("*")) if path.is_file()}


async def diagnose(output: Path):
    import httpx
    from playwright.async_api import async_playwright
    from benchmarks.provenance import provenance

    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        raise RuntimeError("Run npm --prefix frontend run build first")
    output.mkdir(parents=True, exist_ok=False)
    before = provenance(ROOT)
    frontend_before = frontend_hashes()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    assert port != 8765
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["COMPUTERUSE_PORT"] = str(port)
    started = time.monotonic()
    process = browser = page = None
    checks = {}
    sessions = []
    requests = []
    responses = []
    console_errors = []
    console_warnings = []
    page_errors = []
    external_requests = []
    report = {
        "kind": "production_workspace_real_http_e2e",
        "planner_mode": "scripted_demo_no_ai",
        "provider": "demo", "model": None, "model_calls": None,
        "passed": False, "checks": checks, "sessions": sessions,
        "production_frontend_ui_used": True, "production_frontend_ui_validated": False,
        "production_api_and_driver_used": True,
        "headless_workspace_browser": True, "headless_executor_browser": True,
        "native_desktop_used": False, "user_port_8765_used": False,
        "owned_loopback_port": port, "api_mutations": requests,
        "api_responses": responses, "page_errors": page_errors,
        "console_errors": console_errors, "console_warnings": console_warnings,
        "unexpected_external_requests": external_requests,
        "provenance": before, "frontend_build_sha256": frontend_before,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "oracle_hook": "Reuses diagnose_preview_http.serve: traps model calls and reads actual demo DOM on driver close; production execution is unchanged.",
        "limits": "Fixed demo only; no AI reasoning, real-site login, general desktop co-working or release Gate is validated.",
    }

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise AssertionError(name)

    with (output / "server.log").open("w") as log:
        try:
            process = subprocess.Popen([
                sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port),
                "--fd", str(listener.fileno()),
            ], cwd=ROOT, env=env, pass_fds=(listener.fileno(),), stdout=log, stderr=subprocess.STDOUT)
            report["owned_uvicorn_pid"] = process.pid
            listener.close()
            async with httpx.AsyncClient(base_url=base, timeout=10, trust_env=False) as client:
                async with asyncio.timeout(15):
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError("Owned server exited before readiness; inspect server.log")
                        try:
                            config = await client.get("/api/config")
                            if config.status_code == 200:
                                check("isolated_server_starts_in_demo", config.json()["provider"] == "demo")
                                break
                        except httpx.TransportError:
                            pass
                        await asyncio.sleep(.1)

                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True)
                    context = await browser.new_context(viewport={"width": 1280, "height": 900})

                    async def local_requests_only(route):
                        parsed = urlsplit(route.request.url)
                        if f"{parsed.scheme}://{parsed.netloc}" == base:
                            await route.continue_()
                            return
                        if parsed.hostname not in {"fonts.googleapis.com", "fonts.gstatic.com"}:
                            external_requests.append(route.request.url)
                        await route.fulfill(status=204, body="")

                    await context.route("**/*", local_requests_only)
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    page.on("pageerror", lambda error: page_errors.append(str(error)))
                    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error"
                            else console_warnings.append(msg.text) if msg.type == "warning" else None)

                    def record_request(request):
                        if request.url.startswith(base + "/api/") and request.method == "POST":
                            requests.append({"method": request.method, "path": urlsplit(request.url).path,
                                             "body": request.post_data_json})

                    def record_response(response):
                        if response.url.startswith(base + "/api/"):
                            responses.append({"path": urlsplit(response.url).path, "status": response.status,
                                              "content_type": response.headers.get("content-type", "")})

                    page.on("request", record_request)
                    page.on("response", record_response)
                    loaded = await page.goto(base, wait_until="domcontentloaded")
                    check("production_index_http_200", loaded.status == 200)
                    await page.get_by_role("button", name="試跑示範", exact=True).wait_for()
                    await page.get_by_role("button", name="執行設定", exact=True).click()
                    check("ui_browser_visibility_defaults_false", not await page.locator("#browser-visible").is_checked())

                    async def snapshot(run_id):
                        response = await client.get(f"/api/sessions/{run_id}")
                        response.raise_for_status()
                        return response.json()

                    async def wait_status(run_id, expected, action=None):
                        async with asyncio.timeout(25):
                            while True:
                                session = await snapshot(run_id)
                                if session["status"] == expected and (action is None or
                                        (session.get("pending_action") or {}).get("type") == action):
                                    return session
                                if session["status"] in {"failed", "stopped", "completed"}:
                                    raise RuntimeError(f"Unexpected session outcome: {session['status']}: {session.get('error')}")
                                await asyncio.sleep(.07)

                    async def start_demo(label):
                        async with page.expect_response(lambda response: response.request.method == "POST"
                                and urlsplit(response.url).path == "/api/sessions") as created:
                            await page.get_by_role("button", name="試跑示範", exact=True).click()
                        response = await created.value
                        check(f"{label}_ui_create_http_200", response.status == 200)
                        session = await response.json()
                        check(f"{label}_request_is_headless_demo", session["provider"] == "demo"
                              and session["target"] == "browser" and session["browser_visible"] is False
                              and session["approval_mode"] == "always")
                        await wait_status(session["id"], "awaiting_approval", "type")
                        await page.get_by_role("button", name="允許這一步", exact=True).wait_for()
                        await page.wait_for_function("""id => {
                          const image = document.querySelector('.screen-area img');
                          return image?.getAttribute('src') === `/api/sessions/${id}/view`
                              && image.naturalWidth > 0;
                        }""", arg=session["id"])
                        image = await page.locator(".screen-area img").evaluate("image => ({src:image.getAttribute('src'),width:image.naturalWidth,height:image.naturalHeight,alt:image.alt})")
                        check(f"{label}_active_img_decoded_before_approval", image["width"] == 1280 and image["height"] == 800)
                        return session["id"], image

                    async def final_image(run_id, label):
                        await page.wait_for_function("""id => {
                          const image = document.querySelector('.screen-area img');
                          return image?.getAttribute('src')?.startsWith(`/api/sessions/${id}/screenshot?v=`)
                              && image.naturalWidth > 0;
                        }""", arg=run_id)
                        value = await page.locator(".screen-area img").evaluate("image => ({src:image.getAttribute('src'),width:image.naturalWidth,height:image.naturalHeight,alt:image.alt})")
                        check(f"{label}_last_screenshot_decoded", value["width"] == 1280 and value["height"] == 800)
                        return value

                    async def closed_oracle(run_id):
                        async with asyncio.timeout(6):
                            while True:
                                response = await client.get(f"/__proof/oracle/{run_id}")
                                response.raise_for_status()
                                value = response.json()
                                if value["task_done"]:
                                    return value
                                await asyncio.sleep(.05)

                    first_id, first_image = await start_demo("completed_run")
                    await page.screenshot(path=output / "workspace-active-desktop.png", full_page=True)
                    approvals = []
                    for action, label in [("type", "輸入文字"), ("click", "點擊")]:
                        pending = await wait_status(first_id, "awaiting_approval", action)
                        await page.locator(".approval-card h3").filter(has_text=label).wait_for()
                        approvals.append({"type": action, "step": pending["step"],
                            "preview_width": await page.locator(".screen-area img").evaluate("image => image.naturalWidth")})
                        async with page.expect_response(lambda response: response.request.method == "POST"
                                and urlsplit(response.url).path == f"/api/sessions/{first_id}/approve") as approved:
                            await page.get_by_role("button", name="允許這一步", exact=True).click()
                        check(f"ui_{action}_approval_http_200", (await approved.value).status == 200)
                    completed = await wait_status(first_id, "completed")
                    first_last = await final_image(first_id, "completed_run")
                    await page.locator(".completion-card").wait_for()
                    oracle = await closed_oracle(first_id)
                    actual = oracle.get("oracle") or {}
                    check("independent_demo_dom_oracle", actual.get("name") == "ComputerUSE"
                          and actual.get("result") == "測試任務已完成：歡迎，ComputerUSE。"
                          and actual.get("result_hidden") is False)
                    sessions.append({"purpose": "completion", "id": first_id, "final_status": completed["status"],
                        "active_image": first_image, "last_image": first_last, "approvals": approvals,
                        "session": completed, "fixture_oracle": actual})
                    await page.set_viewport_size({"width": 390, "height": 844})
                    check("mobile_has_no_horizontal_overflow", await page.evaluate("document.documentElement.scrollWidth") == 390)
                    await page.screenshot(path=output / "workspace-completed-mobile.png", full_page=True)
                    await page.set_viewport_size({"width": 1280, "height": 900})

                    await page.get_by_role("button", name="新任務 ↗", exact=True).click()
                    await page.get_by_role("button", name="執行設定", exact=True).click()
                    check("new_task_resets_browser_visibility_false", not await page.locator("#browser-visible").is_checked())
                    second_id, second_image = await start_demo("stopped_run")
                    async with page.expect_response(lambda response: response.request.method == "POST"
                            and urlsplit(response.url).path == f"/api/sessions/{second_id}/stop") as stopped_response:
                        await page.get_by_role("button", name="停止", exact=True).click()
                    check("ui_stop_http_200", (await stopped_response.value).status == 200)
                    stopped = await wait_status(second_id, "stopped")
                    second_last = await final_image(second_id, "stopped_run")
                    second_oracle = await closed_oracle(second_id)
                    check("stopped_before_any_execution", not any(e["kind"] == "action" for e in stopped["events"]))
                    sessions.append({"purpose": "stop_before_action", "id": second_id, "final_status": stopped["status"],
                        "active_image": second_image, "last_image": second_last, "session": stopped,
                        "fixture_oracle": second_oracle.get("oracle")})
                    report["model_calls"] = second_oracle["model_calls"]
                    check("zero_model_calls", second_oracle["model_calls"] == 0)
                    for run_id in (first_id, second_id):
                        stream = [r for r in responses if r["path"] == f"/api/sessions/{run_id}/view"]
                        check(f"{run_id}_real_multipart_http_200", bool(stream) and all(r["status"] == 200 and
                              r["content_type"].startswith("multipart/x-mixed-replace; boundary=computeruse-frame") for r in stream))
                    check("no_api_http_errors", all(r["status"] < 400 for r in responses))
                    check("no_console_errors_or_warnings", not console_errors and not console_warnings)
                    check("no_page_errors", not page_errors)
                    check("no_unexpected_external_requests", not external_requests)
                    check("runtime_sources_unchanged", before["source_sha256"] == provenance(ROOT)["source_sha256"])
                    check("frontend_build_unchanged", frontend_before == frontend_hashes())
                    await browser.close()
                    browser = page = None
        except Exception as error:
            report["failure"] = {"type": type(error).__name__, "message": str(error)}
            if page and not page.is_closed():
                try:
                    await page.screenshot(path=output / "workspace-failure.png", full_page=True)
                except Exception:
                    pass
        finally:
            listener.close()
            if browser:
                await browser.close()
            if process and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
            report["owned_server_exited"] = process is not None and process.poll() is not None
            report["owned_server_exit_code"] = process.returncode if process else None
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            report["passed"] = bool(checks) and all(checks.values()) and "failure" not in report and report["owned_server_exited"]
            report["production_frontend_ui_validated"] = report["passed"]
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "workspace-live-preview-proof")
    args = parser.parse_args()
    if args.serve:
        from scripts.diagnose_preview_http import serve
        assert args.port and args.fd is not None
        serve(args.port, args.fd)
        return 0
    report = asyncio.run(diagnose(args.output.resolve()))
    print(json.dumps({key: report.get(key) for key in
        ("passed", "failure", "model_calls", "checks", "owned_loopback_port", "owned_server_exited")},
        ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
