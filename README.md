# ComputerUSE

**A local-first, model-switchable workspace for observable computer-use experiments.**

ComputerUSE turns browser DOM, OCR and accessibility observations into text and element IDs, lets a selected model propose an action, and shows the user what happened. The workspace includes per-step approval, pause, stop, user input, isolated Chromium sessions and live browser previews.

**Public source preview · MIT licensed · Not a production-ready autonomous desktop agent.**

[Validation evidence](docs/evidence/README.md) · [Architecture](docs/architecture.md) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md) · [Privacy](docs/PRIVACY.md) · [License](LICENSE)

## What is available

- Isolated browser navigation, typing, clicks, scrolling, tabs and explicitly allowed file uploads. Downloads stay in task-specific output directories.
- Protocol adapters for OpenAI Chat Completions, Anthropic Messages, Gemini, Ollama and compatible endpoints. A protocol adapter is not a guarantee that every model completes tasks reliably.
- Text-only planning from structured observations; optional screenshot input for compatible vision models. OCR can run locally.
- A React/TypeScript workspace and Python/FastAPI service bound to loopback by default. API keys are held in service memory, not committed to source or returned in the public configuration response.
- Synthetic task fixtures, independent benchmark oracles and candidate SQLite/Policy/ActionGateway/Hermes components.

## Current boundaries

The production-facing workspace still uses the prototype in-memory Run. The durable core and Hermes integration are candidates, not yet the sole execution path. Independent verification in benchmarks and the candidate core must not be confused with a general verifier for every workspace task.

Native desktop execution remains disabled until independent-input requirements are met. Windows/Linux native desktop certification, mobile support and restart recovery are not claimed. Browser fixture tests and mocked-model tests do not establish general real-model task reliability. Historical model results and newly executed public validation are separated in [the evidence index](docs/evidence/README.md).

## Quick start

Prerequisites: Git, Python 3.11+ (Python 3.12 is used for public validation), uv, and Node.js 22+ with npm. Install these tools from their official distributions.

```bash
git clone https://github.com/Magician-Bee/computer-use-agent.git
cd computer-use-agent
bash start.sh
```

Open `http://127.0.0.1:8765`. First startup installs the locked Python/frontend dependencies and Chromium. The built-in demo uses fixed steps on a local page, with no model calls or desktop control.

For a Windows browser-only setup, run in PowerShell:

```powershell
uv sync --frozen
uv run --frozen python -m playwright install chromium
npm --prefix frontend ci
npm --prefix frontend run build
uv run --frozen python -m uvicorn server.app:app --host 127.0.0.1 --port 8765
```

This is a browser-only setup recipe, not Windows desktop certification. Platform coverage is recorded in the validation evidence. macOS-specific accessibility and OCR facilities are optional platform capabilities.

Choose a provider and a model in the settings panel. Start with synthetic data and per-step approval. You do not need Hermes, model weights or cloud credentials to run the local demo. The optional Hermes experiments require the separately installed, pinned environment described in [the integration notes](docs/hermes-integration.md).

## Privacy and operating scope

Local-first does not mean every configuration is offline. The selected planner receives task text and observation text. With vision enabled, it also receives screenshots. Use only data you are authorized to send to that provider.

Do not expose the control API to the Internet. Origin/Host checks are not multi-user authentication or an operating-system sandbox. Use test accounts and disposable browser sessions; do not run unattended financial, account-management or destructive tasks. Task exports may contain private text and must be reviewed before sharing.

## Validation

```bash
uv sync --frozen
uv run --frozen python scripts/check_public_privacy.py
uv run --frozen python -m pytest tests/test_providers.py tests/test_task_store.py tests/test_task_store_pause.py tests/test_public_release.py -q
npm --prefix frontend ci
npm --prefix frontend run test:install-browser
npm --prefix frontend test
```

The public validation workflow checks the source package, selected provider/storage contracts, frontend build and isolated mock-API browser tests. It does not use cloud keys, run a real model, certify native desktop isolation or execute the entire historical suite. See [machine-readable evidence](docs/evidence/public-validation.json) for exact commands and outcomes.

## Project map

| Area | Purpose |
| --- | --- |
| `frontend/` | Workspace, provider settings and UI-contract tests |
| `server/agent.py` | Current prototype observation/action loop |
| `server/providers.py` | Provider request/response adapters |
| `server/drivers.py` | Browser driver and legacy desktop experiments |
| `server/core/` | Candidate task, policy, action and verifier components |
| `benchmarks/` | Synthetic tasks and independent success checks |
| `tests/` | Contract and regression tests |
| `docs/` | Technical notes, evidence and implementation status |

## Development priorities

Connect the workspace to the single durable execution gateway; improve real-model task completion and recovery; complete installation and platform validation; extend privacy-safe evaluation coverage. Detailed scope is retained in [PLAN.md](PLAN.md), [ACCEPTANCE.md](ACCEPTANCE.md) and [the implementation audit](docs/plan-audit.md). These are development records, not claims that all milestones have passed.

## License and dependencies

Original project code and documentation are released under the [MIT License](LICENSE). Third-party packages, optional integrations and model weights keep their own licenses. In particular, optional Ultralytics/YOLO components require a separate license review; they are not relicensed by this repository. See [third-party notices](THIRD_PARTY_NOTICES.md).

## 繁體中文摘要

ComputerUSE 是本機優先、可切換模型的電腦操作工作台，將 DOM、OCR 與可存取性資訊轉成文字及元素 ID，提供人工核准、暫停、停止與即時畫面。此版本是整理後的公開原始碼預覽版，不代表通用 AI 任務或跨平台桌面已完成驗收；目前以隔離瀏覽器為主要使用範圍。
