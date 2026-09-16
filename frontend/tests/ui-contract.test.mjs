import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, readFile } from 'node:fs/promises';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const fixturePath = fileURLToPath(new URL('./mock_api.py', import.meta.url));

async function startMockApi() {
  const child = spawn(process.env.COMPUTERUSE_TEST_PYTHON || 'python3', [fixturePath], {
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', chunk => { stderr += chunk; });
  const origin = await new Promise((resolve, reject) => {
    let stdout = '';
    const timeout = setTimeout(() => {
      child.kill();
      reject(new Error(`Mock API did not start within 10 seconds. ${stderr}`));
    }, 10_000);
    child.once('error', error => {
      clearTimeout(timeout);
      reject(new Error(`Cannot start Python test fixture. Install Python 3.9+ or set COMPUTERUSE_TEST_PYTHON. ${error.message}`));
    });
    child.once('exit', code => {
      clearTimeout(timeout);
      reject(new Error(`Mock API exited (${code}). ${stderr}`));
    });
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdout += chunk;
      const line = stdout.split('\n')[0];
      if (!stdout.includes('\n')) return;
      try { clearTimeout(timeout); resolve(JSON.parse(line).origin); }
      catch (error) { child.kill(); reject(error); }
    });
  });
  return {
    origin,
    async close() {
      if (child.exitCode !== null) return;
      await new Promise(resolve => {
        child.once('exit', resolve);
        child.kill('SIGTERM');
        const force = setTimeout(() => child.kill('SIGKILL'), 2000);
        force.unref();
        child.once('exit', () => clearTimeout(force));
      });
    },
  };
}

async function downloadText(download) {
  assert.equal(await download.failure(), null, 'Browser download did not complete');
  return readFile(await download.path(), 'utf8');
}

test('ComputerUSE UI contract — isolated mock API, no models or desktop control', { timeout: 90_000 }, async t => {
  const mock = await startMockApi();
  t.after(() => mock.close());
  let browser;
  try { browser = await chromium.launch({ headless: true }); }
  catch (error) {
    throw new Error(`Cannot launch headless Chromium. Run npm run test:install-browser first. ${error.message}`);
  }
  t.after(() => browser.close());
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const unexpectedExternalRequests = [];
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    if (url.origin === mock.origin) return route.continue();
    // Prevent all outbound network traffic, including the optional font CSS.
    if (!['fonts.googleapis.com', 'fonts.gstatic.com'].includes(url.hostname)) {
      unexpectedExternalRequests.push(url.origin);
    }
    return route.fulfill({ status: 204, body: '' });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(6000);
  const javascriptErrors = [];
  page.on('pageerror', error => javascriptErrors.push(error.message));
  const state = async () => (await page.request.get(`${mock.origin}/_test/state`)).json();
  const scenario = async name => page.request.post(`${mock.origin}/_test/scenario`, { data: { name } });
  const button = name => page.getByRole('button', { name, exact: true });
  // These groups follow one user session intentionally. Stop on the first failed
  // prerequisite so subsequent groups do not produce misleading cascade errors.
  async function check(name, body) {
    let passed = false;
    await t.test(name, async () => { await body(); passed = true; });
    assert.ok(passed, `Stopping dependent UI checks after: ${name}`);
  }
  await page.goto(mock.origin);

  await check('1. Provider selection preserves model, endpoint, and in-memory key drafts', async () => {
    await button('模型與感知').click();
    await page.locator('#model-id').fill('draft-custom-model');
    await page.locator('#base-url').fill('https://draft.mock.invalid/v1');
    await page.locator('#api-key').fill('mock-only-not-a-real-key');
    await button('自訂相容 API').click();
    assert.equal(await page.locator('#model-id').inputValue(), 'draft-custom-model');
    await button('Ollama').click();
    await button('自訂相容 API').click();
    assert.equal(await page.locator('#model-id').inputValue(), 'draft-custom-model');
    assert.equal(await page.locator('#base-url').inputValue(), 'https://draft.mock.invalid/v1');
    assert.ok((await page.locator('#api-key').inputValue()).length > 0);
  });

  await check('2. Test errors are visible and Save sends only accepted config fields', async () => {
    await scenario('test_error');
    await button('測試連線').click();
    const feedback = page.getByRole('status').filter({ hasText: '模擬測試錯誤' });
    await feedback.waitFor();
    // Wait for the intentional CSS smooth scroll, not for a backend operation.
    await page.waitForTimeout(400);
    const box = await feedback.boundingBox();
    assert.ok(box && box.y >= 0 && box.y + box.height <= page.viewportSize().height);
    await button('儲存設定').click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    const saved = (await state()).requests.findLast(request => request.path === '/api/config');
    assert.equal(saved.header, '1');
    assert.equal('has_key' in saved.body, false);
  });

  await check('3. Saved keys can be removed and no browser persistence is used', async () => {
    await button('模型與感知').click();
    await button('清除已儲存金鑰').click();
    await page.getByRole('dialog').waitFor({ state: 'hidden' });
    const latest = await state();
    assert.equal(latest.config.has_key, false);
    assert.ok(latest.requests.some(request => request.method === 'DELETE' && request.header === '1'));
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
  });

  await check('4. Demo entry does not silently replace an already configured provider', async () => {
    const count = (await state()).requests.length;
    await button('切換示範模式').click();
    await page.getByRole('dialog').waitFor();
    assert.equal((await state()).requests.length, count);
    await button('關閉設定').click();
  });

  await check('5. Run sends browser visibility, auto mode, and the upload allowlist', async () => {
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('UI 契約測試：僅使用模擬回應');
    await button('執行設定').click();
    assert.equal(await page.locator('#browser-visible').isChecked(), false);
    // A physical window requires explicit opt-in; the fixture never opens one.
    await page.locator('#browser-visible').check();
    await page.locator('#allowed-files').fill('/tmp/ui-contract.txt');
    await page.getByRole('switch', { name: '自動執行', exact: true }).click();
    await button('開始任務').click();
    await button('暫停').waitFor();
    const run = (await state()).requests.findLast(request => request.path === '/api/sessions');
    assert.equal(run.body.browser_visible, true);
    assert.equal(run.body.approval_mode, 'auto');
    assert.deepEqual(run.body.allowed_uploads, ['/tmp/ui-contract.txt']);
    await scenario('preview');
    await page.locator('.screen-area img').waitFor();
    assert.equal(await page.locator('.screen-area img').getAttribute('src'), '/api/sessions/contract-run/view');
    assert.equal(await page.locator('.screen-area img').getAttribute('alt'), '代理瀏覽器的即時監看畫面');
    await page.waitForFunction(() => document.querySelector('.screen-area img')?.naturalWidth === 1);
  });

  await check('6. Pause/resume, paused intervention, and human handoff remain usable', async () => {
    await button('暫停').click();
    await button('繼續').waitFor();
    await page.locator('#agent-note').fill('暫停期間補充方向');
    await button('送出補充指示').click();
    assert.equal((await state()).sessions[0].status, 'paused');
    await button('繼續').click();
    await button('暫停').waitFor();
    await scenario('input');
    await page.getByText('請在獨立瀏覽器完成登入', { exact: true }).waitFor();
    await page.getByText('遇到登入或驗證時，可直接在此視窗手動完成，再於任務中回覆繼續。', { exact: true }).waitFor();
    await button('暫停').click();
    await button('繼續').click();
    await page.locator('#agent-note').fill('已在測試中完成登入，請繼續');
    await button('送出補充指示').click();
    await page.getByText('請在獨立瀏覽器完成登入', { exact: true }).waitFor({ state: 'hidden' });
    assert.equal((await state()).preview_reads.filter(path => path.endsWith('/view')).length, 1,
      'Status polling must not reconnect the image stream on every observation update');
  });

  await check('7. An acknowledged action cannot be approved twice', async () => {
    await scenario('approval');
    await button('允許這一步').waitFor();
    await button('允許這一步').click();
    await button('確認已送出').waitFor();
    assert.equal(await button('確認已送出').isDisabled(), true);
    assert.equal((await state()).requests.filter(request => request.path.endsWith('/approve')).length, 1);
  });

  await check('8. Completion, JSON export, file download, and missing-file errors work', async () => {
    await scenario('completed');
    await page.getByText('模擬完成結果：契約流程已檢查。', { exact: true }).first().waitFor();
    assert.equal(await page.locator('.completion-card').isVisible(), true);
    assert.match(await page.locator('.screen-area img').getAttribute('src'), /^\/api\/sessions\/contract-run\/screenshot\?v=/);
    assert.equal(await page.locator('.screen-area img').getAttribute('alt'), '瀏覽器的最後觀察截圖');
    const exporting = page.waitForEvent('download');
    await button('匯出任務紀錄').click();
    const exported = await exporting;
    assert.equal(exported.suggestedFilename(), 'computeruse-contract-run.json');
    assert.equal(JSON.parse(await downloadText(exported)).status, 'completed');
    const downloading = page.waitForEvent('download');
    await button('report.txt 1 KB').click();
    const downloaded = await downloading;
    assert.equal(downloaded.suggestedFilename(), 'report.txt');
    assert.equal(await downloadText(downloaded), 'contract download fixture\n');
    await scenario('download_missing');
    await button('report.txt 1 KB').click();
    await page.getByRole('alert').filter({ hasText: '測試檔案已不存在' }).waitFor();
    assert.equal(page.url(), `${mock.origin}/`);
  });

  await check('9. New Task clears prior authorization and desktop hides browser uploads', async () => {
    await button('新任務 ↗').click();
    assert.equal(await page.getByRole('textbox', { name: '任務描述', exact: true }).inputValue(), '');
    await button('執行設定').click();
    assert.equal(await page.getByRole('switch', { name: '自動執行', exact: true }).getAttribute('aria-checked'), 'false');
    assert.equal(await page.locator('#allowed-files').inputValue(), '');
    assert.equal(await page.locator('#browser-visible').isChecked(), false, 'New tasks restore background execution');
    await button('桌面').click();
    assert.equal(await page.locator('#allowed-files').count(), 0);
    await button('瀏覽器').click();
  });

  await check('10. A terminal session error is shown without a matching error event', async () => {
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('UI 契約測試：終止錯誤');
    await button('開始任務').click();
    await button('暫停').waitFor();
    assert.equal((await state()).requests.findLast(request => request.path === '/api/sessions').body.browser_visible, false);
    await scenario('error');
    await page.getByRole('alert').filter({ hasText: '僅有 session.error 的測試錯誤' }).waitFor();
  });

  await check('11. Expired sessions stop polling and refresh the model configuration', async () => {
    await button('新任務 ↗').click();
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('UI 契約測試：遺失任務');
    await button('開始任務').click();
    await button('暫停').waitFor();
    await scenario('missing');
    await page.getByRole('alert').filter({ hasText: '任務紀錄已不存在' }).waitFor();
    await button('連接模型').waitFor();
    const polls = (await state()).poll_count;
    // Exceed two normal 900 ms polling intervals to verify cancellation.
    await page.waitForTimeout(1900);
    assert.equal((await state()).poll_count, polls);
    assert.equal(await button('連接模型').isDisabled(), false);
  });

  await check('12. Desktop window discovery is read-only and unavailable input cannot run', async () => {
    await scenario('desktop_unavailable');
    await page.reload();
    await button('桌面').click();
    await page.locator('#desktop-window option[value="410:710"]').waitFor({ state: 'attached' });
    await page.locator('#desktop-window').selectOption('410:710');
    await page.getByText('背景輸入尚未通過互不干擾驗證', { exact: true }).waitFor();
    assert.equal(await page.locator('.desktop-diagnostics').getByText('尚未通過驗證').count(), 2);
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('背景介面不可用時不能執行');
    const requests = (await state()).requests.length;
    assert.equal(await button('開始任務').isDisabled(), true);
    await page.getByRole('textbox', { name: '任務描述', exact: true }).press('Control+Enter');
    await page.getByRole('alert').filter({ hasText: '請先確認背景輸入可用' }).waitFor();
    assert.equal((await state()).requests.length, requests, 'Discovery, selection and blocked run must not mutate the API');
    assert.ok((await state()).window_reads >= 1);
  });

  await check('13. Explicit window identity reaches desktop requests only after readiness', async () => {
    await scenario('desktop_ready');
    await page.reload();
    await button('桌面').click();
    await page.locator('#desktop-window option[value="411:711"]').waitFor({ state: 'attached' });
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('僅操作選定的模擬視窗');
    assert.equal(await button('開始任務').isDisabled(), true, 'A window must be explicitly selected');
    const requests = (await state()).requests.length;
    await page.locator('#desktop-window').selectOption('411:711');
    assert.equal((await state()).requests.length, requests, 'Selecting a window does not activate it');
    await button('開始任務').click();
    await button('暫停').waitFor();
    const run = (await state()).requests.findLast(request => request.path === '/api/sessions');
    assert.equal(run.body.target, 'desktop');
    assert.equal(run.body.desktop_pid, 411);
    assert.equal(run.body.desktop_window_id, 711);
    await page.getByText('視窗 #711 · PID 411', { exact: true }).waitFor();
    assert.equal(run.body.browser_visible, false);
    assert.deepEqual(run.body.allowed_uploads, []);
    await button('停止').click();
    await button('以這個任務再試一次').waitFor();
  });

  await check('14. Missing windows and discovery errors invalidate selection, including mobile', async () => {
    await button('新任務 ↗').click();
    await page.locator('#desktop-window option[value="410:710"]').waitFor({ state: 'attached' });
    assert.equal(await page.locator('#desktop-window').inputValue(), '', 'A new task does not reuse a window authorization');
    await page.locator('#desktop-window').selectOption('410:710');
    await scenario('desktop_empty');
    await button('重新讀取視窗清單').click();
    await page.locator('#desktop-window option').getByText('目前沒有可列出的視窗', { exact: true }).waitFor({ state: 'attached' });
    assert.equal(await page.locator('#desktop-window').inputValue(), '');
    assert.equal(await page.locator('#desktop-window').isDisabled(), true);
    await scenario('desktop_error');
    await button('重新讀取視窗清單').click();
    await page.getByRole('status').filter({ hasText: '模擬視窗清單讀取失敗' }).waitFor();
    assert.equal(await button('開始任務').isDisabled(), true);
    const output = fileURLToPath(new URL('../output/playwright/', import.meta.url));
    await mkdir(output, { recursive: true });
    await page.screenshot({ path: `${output}/desktop-target-desktop.png`, fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), 390);
    await page.screenshot({ path: `${output}/desktop-target-mobile.png`, fullPage: true });
  });

  await check('15. Background preview reconnects after an error and Stop keeps the final image', async () => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await button('瀏覽器').click();
    await page.getByRole('textbox', { name: '任務描述', exact: true }).fill('背景串流契約：不中斷使用者桌面');
    await button('開始任務').click();
    await button('暫停').waitFor();
    const run = (await state()).requests.findLast(request => request.path === '/api/sessions');
    assert.equal(run.body.browser_visible, false);
    await scenario('preview_error');
    await scenario('preview');
    await page.getByText('截圖暫時無法載入', { exact: true }).waitFor();
    assert.equal(await page.locator('.preview-status').innerText(), '重新連線');
    await scenario('preview_recovered');
    await page.locator('.screen-area img').waitFor();
    await page.waitForFunction(() => document.querySelector('.screen-area img')?.naturalWidth === 1);
    assert.equal(await page.locator('.screen-area img').getAttribute('src'), '/api/sessions/contract-run/view');
    await button('停止').click();
    await button('以這個任務再試一次').waitFor();
    assert.match(await page.locator('.screen-area img').getAttribute('src'), /^\/api\/sessions\/contract-run\/screenshot\?v=/);
    await page.getByText('最後畫面', { exact: true }).waitFor();
  });

  assert.deepEqual(javascriptErrors, [], 'Unexpected browser JavaScript errors');
  assert.deepEqual(unexpectedExternalRequests, [], 'Unexpected attempts to contact external services');
});
