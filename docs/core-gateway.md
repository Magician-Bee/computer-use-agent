# 共用執行入口候選

`server/core/gateway.py` 已把受信任父程序建立的 Policy、ToolManifest、TaskStore、lease 與真實 headless BrowserDriver 接在一起。它目前用於整合驗證，尚未取代工作台 `server/agent.py`；不能宣稱正式工作台已經具有持久恢復或獨立完成裁決。

## 誰可以操作

父程序註冊已啟動的代理瀏覽器、唯一 task／session／resource 身分及固定版本的工具 manifest。只接受 headless BrowserDriver；可見瀏覽器、原生桌面及未實作的其他工具不進入這個候選入口。Manifest 在註冊時序列化保存，輸入實作檔案的 digest 在註冊與執行前核對。

`PolicyRegistry` 固定一個 policy ID 對應的內容。Scope、允許的動作及需要逐次確認的動作都來自父程序，模型不能在工具參數中改寫。Policy digest 同時釘在 SQLite；程序重啟後，用相同 ID 註冊較寬鬆權限會被拒絕。這是父程序內部的能力邊界，不是防止任意本機程式直接操作瀏覽器的 OS 沙箱。

若指定 website_origin，必須在 BrowserDriver 啟動前設定相同的 `configure_allowed_origin`；事後註冊不能補上已經發出的網路請求。此嚴格模式只轉送同源 HTTP，拒絕全部 3xx、WebSocket 與串流，15 秒逾時且不重試；轉送 body 上限 8 MiB。Playwright API 會先在內部緩衝回應，這不是峰值記憶體上限。兩個本機伺服器驗證跨來源 iframe／POST／302／307／WebSocket 的禁止端 TCP／HTTP 接觸皆為零。DNS、WebRTC、瀏覽器背景服務及 OS 層外送隔離沒有因此被認證。未指定網站 scope 的一般瀏覽器不啟用此嚴格模式。

`observe` 透過已註冊 executor 擷取畫面、保存 observation revision 及截圖 digest。截圖本身以 `volatile:` 參照留在記憶體，不會偽裝成可從磁碟恢復的證據。回傳模型前複製 observation，避免下游修改父程序保存的版本；重啟後必須重新觀察。

## 下發與中止

每個 ActionEnvelope 綁定 action payload、task／observation revision、工具版本、scope、lease、fencing token 與 idempotency key。Gateway 先核對政策與當前畫面；目標改變不能偷偷換成另一個 ID 沿用舊 approval。接著由 SQLite 在同一 transaction 消耗 exact-payload approval 並記錄 dispatch，才呼叫 driver。

裸座標動作除了整體文字／縮圖比較，另比對起點附近 64×64 RGB 區域；拖曳也檢查終點。這能抓出整張畫面差異過小、卻在點擊處更換圖示的情況，並容許有限的反鋸齒差異；仍不把影像閾值當成畫面語意的完整證明。

BrowserDriver 的單次綁定 guard 在下一個輸入 IPC 前核對目前 coroutine 確實是該 dispatch 的執行者，並再次查詢 TaskStore 的 task／lease／fence／observation 有效性。另一個 coroutine 不能借用正在拖曳的合法 action 去送出未授權按鍵。

中止先撤銷資料庫權限，再中斷 driver、等待已按下按鍵釋放並關閉這個 task 的瀏覽器，才提交 CANCELLED。已經 FAILED／SUCCEEDED 的 task 也仍會清理 executor，保留原終態。呼叫方取消等待，不會取消清理工作。已送到 Chromium 的命令無法撤回；不宣稱每個瀏覽器事件都能與 SQLite 原子提交。

Driver 回傳僅代表傳輸成功。例外或取消一律保留 outcome_unknown，不能假定點擊完全沒有生效。已知結果可按原 action hash 讀回，不再執行；未知結果會阻止相同 resource 的後續下發，待獨立對帳。

## 保留瀏覽器的暫停

`suspend(task_id, expected_revision)` 先透過 TaskStore 撤銷權限，再取消並等待該 task 的輸入／讀取，釋放拖曳中的滑鼠按鍵與可能按住的快捷鍵，保留背景 Chromium 和游標。按鍵在原來下發輸入的 page 釋放，不能因中途換頁而送至別的 page。清理由 gateway 持有；呼叫方取消等待不會取消清理。釋放失敗時拒絕恢復，須 Stop 清理。

`resume()` 等待暫停清理完成，只回到 REOBSERVE。父程序必須重新取得 lease、進入新的 RUNNING revision 並成功擷取新觀察，才會重新開啟 driver 輸入。舊提案、觀察或 approval 不復活；未知副作用不會重播。Stop 與暫停競爭時仍以 Stop 為優先。

新增 [7 項真 headless 暫停測試](../tests/test_core_gateway_suspend.py)，與 gateway、感知、四工具及 store 暫停回歸合跑為 **84 passed，37.80 秒**，沒有模型或共用桌面輸入。它驗證 driver／核心暫停，不代表 Hermes 對話已能暫停後繼續；現有 bridge 結束會關閉 worker，還需另接 conversation 生命週期與正式 UI。

## 已驗證的鏈路

可重做整合檢查，輸出必須使用新目錄以保留舊結果：

```bash
.venv/bin/python scripts/diagnose_hermes_gateway.py \
  --python /Users/example/.hermes/hermes-agent/venv/bin/python \
  --output artifacts/hermes-browser-core-proof-new
```

這個檢查使用真實固定來源 Hermes、真實 SQLite／Policy／Gateway 及 headless Chromium。模型端是明確標示的固定腳本，真實模型呼叫數為 0。Hermes 透過四個工具提出觀察、中文輸入、儲存及完成候選；父程序另讀取 fixture 儲存值與次數，才建立 Evidence／CompletionVerdict。Hermes 的文字結果仍保持 `completion_verified=false`。

[首輪報告](../artifacts/hermes-browser-core-proof/report.json)完成 6 次腳本 API 往返、2 次真實瀏覽器動作；60 個 pointermove，約 980 毫秒；獨立讀回中文正確且儲存一次，重新開啟 DB 後保留 SUCCEEDED。這份首輪證據早於後續 persistent policy pin／局部影像補強，不能代表那些後續變更已測。最新覆蓋以 [gateway 測試](../tests/test_core_gateway.py)、[driver guard 測試](../tests/test_browser_authority.py)、[policy 測試](../tests/test_core_policy.py)及後續 reviewed 報告為準。

[補強後的 reviewed run2](../artifacts/hermes-browser-core-proof-reviewed-run2/report.json)在 3.755 秒完成相同鏈路：2 次真實動作、60 個 pointermove／983.1 毫秒，DB 重開後保留獨立裁決的 SUCCEEDED；另確認 Hermes 結束已關閉所屬瀏覽器。先前 [reviewed harness 失敗](../artifacts/hermes-browser-core-proof-reviewed/report.json)保留為 passed=false：報告程式在正確清理後還試圖讀取已關閉頁面，後續將擷取移到 verifier、清理前完成；沒有把失敗報告改成成功。

補強後完整 Python／benchmark 測試為 **509 passed、1 skipped**；skip 是需明確啟用的真實 GLM-OCR 測試，本次沒有因此發送額外模型請求。前端先前 16 項測試與正式 HTTP 工作台 27 項檢查仍各自保留；本次核心測試不能替代正式 UI 切換驗收。

之後再加入並單獨通過 2 項 Gateway／網站 scope 整合測試：缺少啟動前 scope 設定會拒絕註冊，已授權 new_tab 可跨過中間 about:blank 並停在同源目標；例外不能沿用到下一個空白頁動作。這兩項是上述完整回歸之後的增補測試。

## 尚未完成

正式 Supervisor／UI 切換、Hermes 暫停後對話恢復、一般任務成功條件設定、使用量 ledger、未知副作用對帳及持久恢復仍在建置。真實小模型接頭與父程序 VerifierRegistry 已在[候選模型鏈路](hermes-model-runtime.md)接通，但尚未完成真模型任務。Fixture verifier 只證明該測試頁的獨立讀回，不能驗收一般電腦任務。Native desktop／Windows 獨立輸入仍未通過，全部 release Gate 維持未通過。
