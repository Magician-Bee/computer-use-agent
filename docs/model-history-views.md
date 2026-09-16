# 可還原的模型歷史資料

更新：2026-09-11。這是候選 Hermes benchmark 的選配傳輸功能，正式工作台尚未啟用。它不增加 planner，也不使用另一個模型摘要對話。

## 執行路徑

Hermes 仍保有完整對話。每次呼叫 LocalModelServer 時，由父程序的 `ModelViewSession.prepare_request()` 建立模型輸入版本；原訊息、輸入版本與還原 manifest 必須先提交到同一份 TaskStore，接著才允許一次 adapter completion。HTTP 接頭獨立重算輸入 hash，並確認除了 `messages` 以外的請求欄位完全相同。準備失敗、取消、保留額度用盡或任務已暫停時，不呼叫模型。

預設 `--context-view full` 維持完整歷史。診斷 fixture 可另行評估：

```bash
.venv/bin/python scripts/benchmark_hermes.py --case profile --context-view lossless --ocr-engine glm_ocr --output artifacts/hermes-lossless-new-run
```

輸出目錄必須不存在。此命令使用既定兩個 Ollama planner 與 GLM-OCR，沿用原始任務、成功條件與 420 秒時限，結果與完整歷史版本分開計分。

## 保留哪些內容

父程序只釘選真正由 ComputerTools 回傳的 JSON。歷史中的工具名稱、call ID、配對與內容 SHA 必須符合，才可引用觀察資料；模型提供的 hash 或 manifest 本身不授權任何事。

- 最新一份觀察完整保留。較早觀察的相同 `text`、`elements`、`tabs` 可引用仍完整存在的來源欄位。
- 大部分相同、只有部分文字改變的舊觀察可使用逐行引用。新增、改變或消失的內容仍保留在歷史中。
- 任務與 system／assistant 訊息不改寫。工具結果的 receipt、checks、errors，以及其他非觀察欄位不被摘要或刪除。
- manifest 留在父程序資料庫。它包含還原所需的原始工具內容，不送給 planner。

欄位引用使用 `$computeruse_observation_ref_v1`，包含來源 `call_id` 與 JSON pointer `path`。文字區塊使用 `parts`，依序串接 `literal` 或來源 `lines:[start,end]`；行號從零開始、右端不含，保留原換行字元。來源是完整且未被替換的訊息，不建立引用鏈。文字比對至多使用 16 份來源，每份最多 10,000 字元、1,024 行；只在實際序列化後省下空間時替換。

`restore_messages()` 檢查 hash、重建原文，再重新執行決定性的投影以核對 manifest。完整訊息結構與工具 `content` 字串必須精確相同。這能驗證資料沒有遺失，不能證明小模型會理解所有引用。

## 持久化與狀態

SQLite schema v4 以新增資料表的方式保留既有 v2／v3 任務、政策與 journal。沒有第二份 TaskStore。每份輸入記錄 task revision；提交前須再次通過 CAS 與允許的任務狀態。模型輸入版本與完成／執行權限無關。

目前只接受明確的 `diagnostic_fixture` 保留模式，預設每任務 16 MiB，計算壓縮內容 bytes，未包含 SQLite 頁面與索引開銷。原始訊息保留上限為 2 MiB，manifest 為 8 MiB，讀回也限制解壓大小。模式與額度不能在重開時默默更改。用盡時拒絕新推理，不刪除舊證據或偷偷切換模式。這不是一般使用者資料的完整保留／刪除政策，正式 UI 啟用前仍需接入該政策。

benchmark 結束時由新開的 SQLite 連線逐份讀回並還原。`context_views` 記錄每次來源／輸入 hash 與字元量；lossless 模式通過還要求 `context_view_integrity_verified=true`，且每次實際 planner inference 都有對應記錄。這項條件不能取代原任務 oracle。

## 目前證據

純 codec 的 74 項測試通過，未呼叫模型。先前 v2-run2 的八份歷史請求在離線轉換後皆可精確還原；Qwen 最後一輪為 61,367→51,809 字元，MiniCPM 為 21,662→14,848 字元。這是輸入大小測量，不是 token、速度或任務成功率結果；歷史來源 hash 的離線釘選也不是新父程序執行權限證據。可用 `.venv/bin/python -m pytest tests/test_context_projection.py -q -s -k saved_real_v2_requests --tb=short` 重做。

真 Hermes 接合報告（local-only evidence; not included: `artifacts/hermes-lossless-real-browser-protocol-v3/protocol-proof.json`）記錄兩次假模型回覆、一次額外觀察、零真模型推理及零輸入操作。每次 adapter 呼叫前，另一個 SQLite 連線已能讀到提交的原文／輸入版本／事件；來源 pin 與真正父程序回覆相符，模型端實際收到引用，最新內容保持逐字相同。此測試的感知文字另加明確的合成診斷資料以確保有可去重內容，13,713→9,100 字元不能當成真模型任務效能。原任務仍失敗，資源已清理。

[ModelViewSession 測試](../tests/test_model_views.py)最終 73 項通過，包含 v3→v4 資料保留遷移、範圍／額度、第二連線暫停／停止 CAS、交易回滾及損壞資料拒絕。這些測試只使用假 adapter，不能取代真模型任務評估。

程式與回歸：[codec](../server/context_projection.py)、[同庫保留](../server/core/model_views.py)、[HTTP 邊界](../server/model_server.py)、[codec 測試](../tests/test_context_projection.py)。真模型結果另外記錄於 [Hermes 模型執行鏈路](hermes-model-runtime.md)。
