# Local model task benchmark

This harness runs the production `server.agent.Run` loop with real Ollama model
calls in an isolated Chromium context. It does not contain a scripted planner,
answer injection, or a fallback solver. The local fixtures record form submissions
and saved changes; Python oracles grade those records independently of model
completion messages. The browser may only access its ephemeral fixture origin.

Cases: activity registration with selects and a checkbox; filtered inventory
editing; supplier retrieval across two tabs. These measure local browser tasks,
not universal desktop reliability.

Requirements: install the project's Python requirements and Chromium
(`.venv/bin/python -m playwright install chromium`), start Ollama, and install the
chosen planner models. Native OCR uses macOS Vision on macOS, or the configured
local OCR engine on other platforms. GLM OCR additionally needs the local
`glm-ocr:latest` model. No cloud key is used.

List tasks and verify state oracles without running a model:

```bash
.venv/bin/python scripts/benchmark_models.py --list
.venv/bin/python -m pytest benchmarks/test_benchmarks.py -q
```

Run one real planner with screenshots disabled and native OCR enrichment:

```bash
.venv/bin/python scripts/benchmark_models.py --model 'qwen3-vl:2b' --case profile --ocr-engine native
```

Run both requested planners, sequentially, using separate GLM OCR:

```bash
.venv/bin/python scripts/benchmark_models.py --model 'minicpm-v4.6:latest' --model 'qwen3-vl:2b' --ocr-engine glm_ocr
```

By default, planners receive DOM plus OCR text and element boxes, with no image.
Add `--no-dom` to remove DOM text/targets and evaluate OCR-only observation.
Add `--vision` only when testing screenshot input to the planner. The report
records these modes explicitly. `--case` and `--model` may be repeated;
`--timeout` limits each case. `--output` selects the JSON artifact path.
Use `--repeat 3` for three independent browser contexts per model/case; repeated
success is a stronger result than a single passing run. Repetition does not add
new layout or task variants, so it does not itself measure generalization.

Reports contain actions, production events, model-call timings, observation
timings, final fixture state, and every oracle check. They are written after each
case so previous results survive a later failure. A request for human input fails
an unattended case. Timeout cases cannot pass. Exit code 0 means all selected
cases passed; 1 means at least one failed.

Reports also include source SHA-256 hashes and dependency versions captured when
the benchmark imports the implementation. `model_claimed_completed` and
`false_completion` distinguish the model's completion claim from oracle success.
The task outcome can pass its oracle even if the agent did not terminate as
completed; always inspect both `passed` and `status`. Older diagnostic reports
may lack this newer provenance metadata.
