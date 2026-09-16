# 共用契約與 TaskStore 候選

此模組正在實作 P00／P01／P09／P10 的核心契約。它還沒有接管工作台；目前 UI 的 Run 歷史仍在記憶體，不能宣稱重啟後會自動恢復。

`server/core/contracts.py` 定義 `schema_version=1.0` 的 Task、Step、ActionEnvelope、ObservationRef、Evidence、Approval、ResourceLease、Event、Checkpoint、ToolManifest。未知欄位、錯誤版本、不完整資源身分、無時區時間與不匹配 hash 都會拒絕。ActionEnvelope 沿用現有 Action，只加入執行身分、觀察／任務版本、scope、授權與租約。

`server/core/store.py` 用一份 SQLite 存放任務、事件、動作 journal、授權、觀察、證據、租約與 checkpoint。每次修改與事件追加在同一個 transaction；事件有每任務遞增序號。Task revision 用於樂觀鎖；每個 resource 的 fencing token 持久遞增，舊 worker 的 lease 無法用在重新指派後的資源。

目前未發布的 SQLite candidate 格式是 user_version=4，與 JSON 契約 1.0 分開版本化。v2／v3 可在同一 transaction 新增缺少的表並保留原資料；v3 加入 policy digest，v4 加入[診斷模型歷史保留](model-history-views.md)。v1／未知格式受控拒絕。已有 dispatch 卻沒有 policy pin 的舊任務不能盲目補授權，仍需明確遷移／對帳。正式備份／回退仍待實作。資源 ID 與 executor identity 持久唯一綁定：同一瀏覽器 session 改 ID、網站或帳號，不能另拿一份輸入鎖。真正的裝置／session 身分由受信任 executor registry 提供；檔案系統的符號連結／inode／重疊寫入路徑仍待後續認證。

## 動作與停止

admit_action 在同一 transaction 核對 RUNNING、已持久釘選的 policy_ref、目前精確 scope、lease token／期限、最新觀察及 exact-payload approval，然後先寫入 dispatched。它不呼叫 driver。[候選 Gateway](core-gateway.md) 成功取得 `dispatch=true` 才能下發動作；既有結果回傳 `dispatch=false`。每個輸入 IPC 前的 assert_dispatch_authority 會重查目前 task、lease、fence、觀察與總時間預算。

未回寫結果的 dispatched，在重啟或重新讀取時視為 outcome_unknown。相同 action ID、相同 idempotency key，以及同資源的後續新 action ID 都不能繞過這個未知結果；必須先用獨立觀察或驗證對帳。此機制禁止盲重播，不承諾外部 GUI 副作用可 exactly-once。

request_stop 原子設定 CANCELLING、提高 revision、撤銷 lease 和 approval，並把未結束 dispatch 標成結果不明。實際 driver 的中斷／按鍵放開由 gateway 清理；完成後才提交 CANCELLED。資料庫已撤銷後，舊動作不能重新取得執行資格。

request_pause 以同一 transaction 提交 PAUSED、提高 revision、撤銷所有 task lease／approval，未完成 dispatch 同樣記為 outcome_unknown。Stop／既有終態優先。request_resume 只能從目前 PAUSED revision 進入 REOBSERVE，存在任何 dispatched／未知結果時拒絕恢復；直接 transition 也不能繞過此檢查。重新取得 lease 並拍攝新畫面之前，不恢復輸入權。這是核心狀態契約，尚未接入正式 UI 或 Hermes 對話 continuation。

## 完成與復原

普通 transition 不能寫 SUCCEEDED。專用 commit_verdict 要求 VERIFYING 狀態、當前 revision、所有使用者定義成功條件、對應 verifier 及目前有效的獨立觀察。Planner 證據不可作成功裁決。終態保留剛被驗證的 revision，事件和狀態仍一起提交。

checkpoint 必須完整列出未知副作用，並核對聲稱已驗證的 evidence；已消耗動作數不能恢復成可用預算，也不能放寬原任務的其他預算上限。Token／成本實際消耗仍待 usage ledger 整合。policy_digest 必須與 SQLite 已釘選的 Policy digest 完全相符，不能靠 checkpoint 自行授權。恢復契約強制重新觀察、對帳且不重播 pending action。真正的程序重啟、driver 重建、資源重新驗證及 Hermes continuation 尚待 Supervisor 整合。

## 尚未涵蓋

這是受信任父程序內的資料與 transaction 邊界，不是 OS 沙箱。Policy、工具檔案完整性與輸入前取消檢查已接入候選瀏覽器 Gateway；一般裝置／路徑驗證、verifier 註冊身分、敏感資料保留及正式 UI 事件投影仍待整合。不能把可匯入模組或局部 SQLite 測試當成整個 Gate 通過。
