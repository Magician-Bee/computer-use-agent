# Hermes 小 context 支援：唯讀技術審查

審查日期：2026-09-11。範圍是固定 Hermes commit `646cd1b43e89920bb283dc11f38463204bcac9db`、目前隔離 worker、Ollama adapter 與本機 HTTP 包裝。**本文件只是候選方案，沒有修改 runtime、Hermes 原始碼、64k baseline 或既有成績，也沒有執行模型推理。**

建議先做「明示的小 context、完整歷史、滿額停止」相容模式，再另行評估壓縮。Hermes 已有正式 context 設定與 ContextEngine 擴充介面，但此固定版沒有查到允許小於 64,000 的正式 opt-in；plugin 仍會遇到初始化的硬性門檻。最小相容修改應只作用於專案私有的衍生來源，預設路徑繼續使用原始固定版本。

## 已確認的依據

固定来源 manifest 的 tree SHA256 是 `e2b7f2d5c1ea50813d59426755ce0c801cf84c9605ab35fe2be905d13d65e4d4`。[manifest](../.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db.json) 記錄 `user_profile_copied=false` 與 `source_modified=false`；目前 [verify_source](../server/hermes_bridge.py) 核對所有追蹤檔案，新增未列名檔案或更動原始碼都會被拒絕。

既有 [真模型協定 probe](../artifacts/hermes-model-protocol-probe.json) 記錄 Qwen `qwen3-vl:2b`、實際請求 `num_ctx=64000`、`prompt_eval_count=452`，以及相符 `/api/ps` 項目的 `context_length=64000`、`size_vram=8942245640` bytes，約 **8.94 GB／8.33 GiB**。這是該次載入總量，不能全部算成 KV cache，也沒有比較不同 context 的對照組。這份證據是協定 probe，不能代替完整任務通過率。

| 層次 | 固定來源位置 | 實际行為與影響 |
|---|---|---|
| Context metadata | `agent/model_metadata.py:1613–1654` | `get_model_context_length()` 優先使用正整數 `model.context_length`，其次才查 cache／endpoint。小數值能通過解析；這不代表 agent 能啟動。缺 metadata 的一般回退是 256k，候選整合不能將這種猜測當成真實容量。 |
| 最低支援門檻 | `agent/model_metadata.py:185`；`agent/agent_init.py:1525–1535` | `MINIMUM_CONTEXT_LENGTH=64_000`。ContextCompressor 或 plugin 初始化後，真實 context 小於門檻就拋錯。設定 `compression.enabled=false` 不會略過此检查。 |
| 壓縮觸發下限 | `agent/context_compressor.py:643–670,703–724` | 建構與 `update_model()` 都使用 `max(int(C × threshold),64000)`。只關掉初始化檢查、卻開壓縮，會讓 8k／16k／32k 窗口一直等到至少 64k 才觸發。64k 窗口在預設 50% 下也得到 64k 門檻。 |
| 壓縮內容預算 | `agent/context_compressor.py:143,178,1005–1022,2037–2043` | 摘要最少 2,000 tokens、尾端最少保護數筆訊息，預設 `protect_last_n=20`，實際尾端 floor 至多 8 筆。不能把大型 context 的預算直接套到小模型。 |
| Ollama 原生執行時門檻 | `agent/conversation_loop.py:103–143,859–882` | 若偵測到 `_ollama_num_ctx` 小於 64k 且有工具，會在推理前退出。它與初始化門檻是兩個不同检查。 |
| Ollama 配置 | `agent/agent_init.py:1609–1652` | 支援 `model.ollama_num_ctx`；未明確指定時，偵測值會被 `model.context_length` 上限限制。這是真實記憶體配置需求，不是只改顯示文字。 |
| 關閉自動壓縮 | `agent/turn_context.py:250–315`；`agent/conversation_loop.py:2649–2704,4047` | 預檢、回應後觸發及已分類的 context overflow 恢復都有 `compression_enabled` guard。關閉時 overflow 應明確失敗，不應自動摘要／另開模型呼叫。 |
| 現有 project 邊界 | [bridge](../server/hermes_bridge.py)、[worker](../scripts/hermes_worker.py)、[adapter](../server/ollama_tools.py)、[HTTP 包裝](../server/model_server.py) | bridge／adapter 限制至少 64k；bridge 私有 profile 設定 context 並關閉壓縮；HTTP metadata 與 adapter `num_ctx` 目前由建構值決定。worker 不提供切模型、fallback、壓縮工具或第二個 planner。 |

上述來源皆位於 [固定 Hermes export](../.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/)。行號只對此 commit 有效。

## 正式 hook 能做什麼

1. **`model.context_length`／custom provider 的 per-model 設定**：正式配置，可忠實表示選定窗口，但無法解除 64k minimum。也不能把模型最大支援值、實際載入窗口和壓縮門檻混為一個數字。
2. **`context.engine` + `ContextEngine`**：固定版 [context_engine.py](../.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/context_engine.py) 定義 `update_model`、`update_from_response`、`should_compress`、`compress`、生命週期等介面。`agent_init.py:1450–1535` 支援 plugin 選擇，卻在選擇後對所有 engine 執行同一 minimum check。因此它適合未來實作小窗口管理，**單獨使用不能解決啟動門檻**。回傳 `context_length=0` 躲過 `if _ctx` 也不是誠實支援方案。
3. **`pre_llm_call` plugin hook**：`turn_context.py:318–343` 的回傳內容是附加到 user context；hook 異常只記 warning。它不是可靠的阻擋／token admission 介面，不能用來保證超限請求不發送。
4. **`model.ollama_num_ctx`**：正式設定；若只是把它降到 16k，同時仍對 Hermes 宣稱 64k，會產生不一致，而且原生偵測路徑還有第二個 minimum check。透過 custom adapter 隱藏此差異不符合目標。

## 目前仍缺少「不暗中截斷」保證

Ollama **v0.32.9** 的 `ChatRequest` 已提供頂層 `truncate` 和 `shift`。`routes.go` 在未指定時將兩者視為 true；`prompt.go` 的截斷流程會保留 system 與最近訊息、移除較舊訊息。故完整傳出 history、不在 Python 切片、回傳真實 usage，仍不足以證明模型看到了完整歷史。[API types](https://github.com/ollama/ollama/blob/v0.32.9/api/types.go#L121)、[路由預設值](https://github.com/ollama/ollama/blob/v0.32.9/server/routes.go#L2497)、[prompt 截斷流程](https://github.com/ollama/ollama/blob/v0.32.9/server/prompt.go#L18)

目前 adapter 的 `/api/chat` body 未指定兩者。本輪 baseline 不修改；報告應保留這項限制，而不是事後將既有結果標成「已證明無截斷」。下一個候選版本應明確送 `truncate:false`、`shift:false`，並針對實際 engine 路徑驗證超限拒絕行為。僅查看 `/api/show` 的模型最大窗口不能驗證 runner 是否遵守這些設定。Ollama 的 image token 計數在該版 `prompt.go` 也標示為 heuristic；因此第一階段應限定 `vision=false` 的 context conformance，VLM 計量另測。

Hermes 本身還會修補 tool pair、移除 thinking-only turns、規範空白與 JSON（`conversation_loop.py:798–854`）。這些不是依窗口丟歷史的策略，但審計應分別記錄「原始對話」和「實際 API view」的訊息數／hash，不能聲稱位元組完全相同。

## 建議的最小候選：`bounded_context_v1`

這是**提案，未實作**。第一階段不提供自動壓縮，不增加模型／planner 迴圈。小窗口可完成能容納的任務；不足時明確中止並保留原始 Task／Evidence／未決副作用，不能宣告完成或重新執行過往動作。

### 單一 context 配置與驗證

父程序建立一份不可由模型修改的配置：`requested_context`、`effective_context`、`model_max_context`、`max_output_tokens`、`overflow_mode=stop`、`compression_enabled=false`、`compatibility_id`。其中 effective 值必須是父程序明示選定並經 provider 核對的 C；不足就拒絕，不能偷偷回退到較小值。

- 私有 Hermes `model.context_length`、HTTP `/v1/models`、adapter `options.num_ctx`、worker ready 訊息全部使用相同 C。若設定原生 `model.ollama_num_ctx`，也必須是同一 C。
- `/api/show` 確認本機模型與最大值 M，要求 C ≤ M；載入後核對相同模型／digest 的 `/api/ps` 實際窗口，並獨立記錄 `size_vram`。核對失敗要分類為容量配置錯誤，不把窗口偽裝成已合格。
- 精確 token preflight 若可用，應計算 **adapter 實際轉出的 messages + renderer／prefill + 輸出保留量**。不能直接把整個 OpenAI `tools` JSON 當成模型的 prompt；現有 adapter 只把完整 schema 用於 grammar，語言提示只帶精簡工具說明。
- 無精確 tokenizer 時，Hermes `len/4` 粗估只可用來提早拒絕或提示，不可作為「一定容納」的證明。最後邊界仍是經測試的 `truncate:false`／`shift:false` 和服務端超限錯誤。prompt 能容納但生成耗盡窗口時，也不得接受不完整工具 JSON。
- 小 context 沒有通用的安全最小值。先把 8,192／16,384／32,768 作測試參數；system、四工具協定、當前完整 observation、必要歷史和輸出保留量不能容納時，此任務便不符合該窗口。

### Hermes 衍生來源的最小修改

保留原始 `MINIMUM_CONTEXT_LENGTH=64000` 常數與未啟用 opt-in 的行為。不要將全域常數改成 0 或較小值：它被 `from ... import` 複製到數個模組，還兼任壓縮下限、auxiliary 及其他 provider 的門檻。

候選 patch 只新增一個父程序可建立的 `bounded_context_v1` 設定檢查，並修改兩個入口：

1. `agent_init.py` 的 minimum check：僅當明示相容模式、真實 C 與設定一致、壓縮關閉、provider 為本專案 custom loopback route、工具恰好四個 parent gateway tools、無 fallback 時，允许 C <64k；其他情況沿用原拒絕。記錄已核對的 C 到專用 agent 屬性。
2. `conversation_loop.py::_ollama_context_limit_error`：對相同已核對的 bounded profile，確認偵測到的 runner C 若存在必須與配置一致；只移除「一律至少 64k」的政策限制，不略過不一致檢查。若無原生偵測資訊，容量驗證由 adapter 的真實請求與 ps 證據負責，不能自行推測。

`model_metadata.py` 的正數設定優先序可保持不動。內建 compressor 可以繼續追蹤 usage，但它的 64k trigger 在此模式是**禁用且不適用**，不能在 UI 顯示成小窗口的有效壓縮門檻。worker 應在建構後 assert `compression_enabled is False`、C 正確、四工具集合不變、無 fallback；任何模型切換、手动 compaction 或不同 context engine 都需要另一個經審查的模式，不直接繼承 opt-in。

相容來源應由「已核對的 base export + 固定 patch」產生新的專案私有唯讀 copy，記錄 base commit/tree digest、patch digest、衍生 tree digest、相容模式 ID。`verify_source` 必須新增**明示的衍生來源驗證分支**，逐檔核對，不能放寬原檢查或覆寫原 manifest。worker 回報实际載入路徑與相容身分。不得編輯 `/Users/example/.hermes/hermes-agent` 或使用者設定；也不建議 import 後對各模組常數做未列入證據的 monkeypatch。

### Overflow 必須保留語意

adapter 把確認過的 context overflow 轉為專用錯誤；HTTP 包裝回傳固定、安全的 `error.code=context_length_exceeded` 和對應狀態，不回傳含 prompt 的原服務錯誤。此固定版 `agent/error_classifier.py:1125` 辨識該 code，`conversation_loop.py:2649–2704` 在壓縮關閉時會停止，不走自動摘要。当前包裝將所有 adapter 例外合併成 generic error，這點也需調整。

父程序把這次 runtime 結束記成 `context_budget_exhausted`／需要明示恢復，不自動開第二個 Hermes conversation、不降 C 重試、不清除歷史或重放 pending action。SQLite 的 `outcome_unknown`、lease／approval 撤銷與 external verifier 成功證據仍由原單一 authority 管理。

## 後續若要自動壓縮：影響範圍較大

應另做 context engine 候選，不能順便打開目前內建 compressor：

- `threshold_tokens` 必須小於 C 並扣除 system、当前 observation、工具協定、預留輸出；建構與 `update_model()` 要同樣更新。`tail_token_budget`、2k 摘要 floor、尾端至少 3–8 筆、2000-token 回應下限及 4096-token 粗估容許增幅都要重新計算，不只改 64k 常數。
- `conversation_compression.py` 的 auxiliary minimum、預設 `abort_on_summary_failure=false`、舊 tool output pruning、摘要失敗後的靜態 fallback／中段刪除，都與「不暗中截斷」有關。`ContextCompressor.compress()` 先 pruning，再產生摘要；即使設成摘要失敗 abort，也不能未經測試就宣稱原始工具輸出完全保留。
- 摘要生成需要額外模型呼叫；目前 adapter 的輸出協定固定為四工具或 final，不是通用 summarizer。不能將此當成免費或未記錄呼叫，也不能把摘要當成新的操作權限。應明示紀錄來源事件範圍、前後 token／訊息量、丟棄與摘要項目、使用模型、費用和取消。
- ContextEngine 的 `get_tool_schemas()` 必須回空集合，避免默默增加第 5 個工具或另建資料庫作第二個 TaskStore。摘要只是可丟棄的模型 view；使用者目標、scope、policy、approval、未決副作用與驗證結果仍以父程序 SQLite 為準。
- 通用 provider 範圍還包括 `run_agent.py:656–689` 的 LM Studio 最低 context 預載、model switch、auxiliary、fallback、cron／其他進程。第一階段 custom loopback + 無 fallback 不會走這些分支；這不是它們已支援小 context 的證明。

## 變更與必要測試矩陣

| 項目 | 預計修改／測試 | 必須看到的結果 |
|---|---|---|
| 私有相容來源 | `agent_init.py`、`conversation_loop.py` 的小範圍 patch；bridge 衍生 manifest 驗證 | 原始 16k 仍拒絕；新 opt-in 16k 能初始化；未啟用 opt-in 的 64k 行為不變；任一來源／patch／來源路徑不符即拒絕。 |
| 設定一致性 | bridge、worker、adapter、model_server 使用同一 C | 8k／16k／32k／64k 真實值一致；metadata 小於 C、未知上限、雲別名、設定值與 runner 不同都拒絕；沒有假報 64k。 |
| 禁止截斷 | Fake HTTP 契約 + 後續明示的本機 runner conformance | 每個實際 request 有 `truncate:false/shift:false`；刻意超限時保留早期 sentinel 與完整歷史，回 overflow 而非產生忽略早期資料的 tool call。需要檢查实际 runner，不只 mock。 |
| 輸入與輸出上限 | C−R 邊界、單一超大 system、單一 observation、長 OCR／中文／emoji、輸出撞窗 | 超限明確終止；`done_reason=length` 或未完成 JSON 不送 gateway；粗估不得假装精確 token count。 |
| Overflow 路由 | Fake provider 錯誤 → model_server → 真 Hermes worker | error code 被分類为 context overflow；壓縮關閉、無 summary／fallback／自動縮 C；模型呼叫與重試數可核對。 |
| 對話保持 | 完整 tool_call/result 群組、最新使用者修正、早期目標、模型回應 hash | 沒有基於 context 丟歷史或未記錄的新增對話；API sanitation 與 adapter 格式轉換有獨立記錄。 |
| 安全與停止 | 正在 HTTP、即將 overflow、已有 unknown action 時 Stop | 取消可關閉 HTTP；工具不重送；approval／lease 不被新 context 復活；外部驗證仍是唯一成功入口。 |
| 64k 回歸 | 固定舊 source／config／fixture 的獨立報告 | 舊 baseline 原檔保留；新版本與新 C 的數字分組，不覆寫或合併成改善率。 |
| 真記憶體比較 | 同模型 digest、相同 prompt／輸出預算／樣本，序列測 C；保存 request＋ps | 同時報載入時間、實際 context、`size_vram`、processor/offload、結果與所有失敗；不能從目前單點 8.94 GB 推導預期省量。 |

此審查沒有執行上述新測試。它不宣告小 context 已可用、不宣告通過任何 Gate，也不更動正在執行的 64k benchmark。
