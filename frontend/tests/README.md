# Headless UI contract checks

These tests run the production React build against a temporary mock API. They do
not use the real backend, Ollama, cloud credentials, native apps, or a visible
browser window. All outbound browser requests are intercepted; optional remote
font requests receive an empty response.

## Requirements and commands

- Node.js 20+ and npm.
- Python 3.9+ (`python3`) for the standard-library mock server.
- Chromium for the exact Playwright version pinned in `package.json`.

```sh
cd frontend
npm ci
npm run test:install-browser
npm test
```

`npm test` builds the application first. The mock API chooses an available
loopback port, so it does not conflict with an existing ComputerUSE server.
Playwright uses its standard shared browser cache; the install command reuses a
matching Chromium downloaded by Python Playwright when present. On Linux hosts
missing browser system libraries, run `npx playwright install --with-deps chromium`.

To choose a specific Python installation:

```sh
COMPUTERUSE_TEST_PYTHON=/path/to/python3 npm test
```

## Coverage

The 15 sequential groups verify provider draft preservation; visible connection
errors and accepted config payloads; saved-key removal; safe demo entry; run
options and browser upload allowlists; pause/resume and intervention; human
handoff; duplicate approval protection; completion and downloaded contents;
per-run authorization resets; terminal errors; and expired-session recovery.
Desktop checks additionally cover read-only window discovery, fail-closed capability
diagnostics, explicit PID/window authorization, stale selection removal, and the
390 px layout. Node reports 16 passing tests when successful: the parent suite
plus its 15 workflow groups.

Browser preview checks verify default background execution, explicit physical
window opt-in, stable `/view` multipart image requests, interrupted-stream
recovery, and final `/screenshot` selection after completion or Stop. The image is
a synthetic 1 px fixture; these checks do not claim actual pointer movement.

The scenarios are intentionally a UI contract harness, not a second copy of the
production agent. They cannot establish real-site compatibility, model reasoning
quality, OCR accuracy, native desktop behavior, actual uploads, or provider API
compatibility. Real backend tests and local-model benchmarks cover those layers
separately. A missing download and an expired session intentionally return HTTP
404 and a simulated stream interruption returns 503; these are expected, handled UI cases.
