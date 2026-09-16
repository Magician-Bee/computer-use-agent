# Hermes 接入狀態

目前已有 **真實 Hermes conversation loop 與專案父程序的 IPC 候選接頭**，並在獨立測試中串接 SQLite、Policy、ActionGateway、真實 headless 瀏覽器與 fixture verifier。工作台仍執行 `server/agent.py` 的 prototype loop；正式主迴圈切換尚未完成，發布 Gate 未通過。

## 安裝隔離

從本機已存在的 Hermes Git repository，匯出 commit `646cd1b43e89920bb283dc11f38463204bcac9db` 的已追蹤檔案，沒有更新或修改既有安裝。匯出 tar 的 SHA-256 是 `6cc5197599cbb91bdcaa9f7590d641e97c1e2cc8c190a09469a5297eb41345eb`；來源在 `.tools/hermes/<commit>`，不包含使用者的 `.env`、config、auth 或 state.db。

`scripts/prepare_hermes.py` 可重做匯出；需明確給 repository 與完整 commit，拒絕不安全路徑及越界連結。Bridge 啟動前核對全部檔案／連結與固定 tree digest；新增、缺失或被修改的檔案都會拒絕。這是原始碼快照，Python 相依環境目前仍使用本機既有 Hermes venv，因此還不是可攜式發行包。

`server/hermes_bridge.py` 每次啟動專案私有 `.runtime/hermes/run-*` 設定目錄，僅繼承 PATH、HOME、TMPDIR、LANG，不繼承 API 金鑰或代理環境變數。標準 HOME 保持原值，Hermes 使用獨立的 HERMES_HOME。新程序以 `-I -B` 執行；不呼叫 Hermes CLI，不載入使用者 session database、記憶或 context files。來源 export 含 `.env` 時拒絕啟動。

## 接口與範圍

Hermes 只拿到 `computer_observe`、`computer_act`、`computer_finish`、`computer_ask_user` 四個 tool。每個呼叫帶遞增序號回到父程序；父程序才負責授權、執行與真實狀態。子程序的 registry 額外阻擋其他工具。協定通道和 Hermes 診斷 stdout 分開。

目前候選只接受明確的 loopback model gateway。子程序的 Python socket audit hook 阻止連到其他端點；此限制用於控制這份受信任 Python 執行路徑，**不是 OS 沙箱或任意惡意程式的隔離保證**。未設置雲端備援。父程序收到文字結果仍一律標示 `completion_verified=false`。

停止會中止正在等待的父端 gateway coroutine，呼叫 `cancel_gateway` 回收實際輸入，再等候子程序結束。真正驅動整合必須提供能中斷並清理按鍵／租約的 callback。一次 Bridge instance 只能啟動一個 conversation，並行啟動會拒絕。

已明確停用 Hermes 在 API 錯誤時寫完整 request dump 的 hook；單純 `save_trajectories=False` 並不足夠。暫存 profile 預設結束即刪，只有明確的 `retain_profile=True` 診斷會保留私有 log。子程序 outcome 保留失敗、中斷、Hermes 自述完成、退出原因與 token usage，不能以收到 result 封包就判成成功。

這版 Hermes 強制至少 64,000 token context，16,384 設定會直接拒絕初始化。早期協定診斷使用固定回覆伺服器；legacy Ollama planner 的 16,384 配置與此不同。後續[本機模型核心整合](hermes-model-runtime.md)已對 Qwen／MiniCPM 使用並實際核對 64,000，未降低或假報容量。最新兩個 profile 均未通過；上下文累積與本機資源仍需改善，不能以成功初始化代替任务能力。

## 已執行的證據

```bash
.venv/bin/python scripts/diagnose_hermes_bridge.py \
  --python /Users/example/.hermes/hermes-agent/venv/bin/python
```

`artifacts/hermes-protocol-conformance-reviewed.json`：真實 Hermes、5 次本機 SSE API 往返、3 次父程序工具呼叫。固定回覆伺服器先回傳未提供的 `terminal` 工具，Hermes 拒絕後才進入 observe → act → finish。父程序未收到 terminal；未驗證的完成提議維持未驗證。

`artifacts/hermes-protocol-fault-conformance.json`：第五次請求故障注入 HTTP 400，父端確實收到 `failed=true`、`hermes_completed=false`；request dump 數量為 0，合成隱私標記沒有寫入 profile。重做時加 `--fail-last --output artifacts/hermes-protocol-fault-conformance.json`。這裡的 `passed` 表示正確回報故障，不能當作電腦任務成功。

此證據標為 `scripted_model_protocol_conformance`，**真實模型呼叫 0、真實電腦動作 0**。它不計入小模型成功率或任何 Computer Use 任務驗收。

新增 [Hermes → 共用核心 → 真實瀏覽器整合](core-gateway.md)：`scripts/diagnose_hermes_gateway.py` 透過 Hermes 提出兩個真實瀏覽器動作，父程序獨立讀回中文儲存值及次數後寫入 verdict，並確認重新開啟 DB 後結果仍存在。此鏈路仍是固定腳本模型（0 真實模型呼叫），不取代 Qwen／MiniCPM 的失敗基線，也不表示 fixture verifier 已擴展為一般任務 Verifier。

再新增的[四工具／模型核心整合](hermes-model-runtime.md)已接父程序 VerifierRegistry、真實初始觀察與操作後畫面，實際呼叫指定模型；其協定測試、初始接合失敗、修正後 0/2 任務結果均分開留證。本文上述腳本協定數字仍僅代表各自原版本。

## 尚需完成

- 將已驗證的候選 TaskStore/EventStore、Policy／租約／授權及 Gateway 接到正式 Supervisor 和工作台。
- 將 Hermes 接成工作台的主要決策流程，避免兩個迴圈同時規劃或操作。
- 接上純文字／不原生支援 tool calling 模型的協定轉接；保留來源模型與 inference 預算。
- 驗證暫停、補充指令、模型串流中止、程序崩潰復原和明確外部完成判定。
- 以現有兩個規劃模型及 GLM-OCR 重跑完整矩陣。現有 0/6 失敗基線保留。
