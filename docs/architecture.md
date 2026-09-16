# ADR 0001：唯一狀態與唯一執行出口

狀態：採用；正在實作，尚未取代現有 prototype Run。適用 P00.04／P01／P02。產品完整範圍仍依原始規劃，這份 ADR 只決定執行核心的責任。

## 決策

SQLite 是唯一 TaskStore、EventStore、action journal、approval 與 resource lease 的儲存位置。所有狀態修改與對應事件在同一 transaction 提交。UI 讀取狀態投影；Hermes 保留對話上下文，但不擁有第二份產品任務終態或 session database。

Hermes 是主要決策迴圈，僅透過四個父程序工具觀察、提出動作、詢問使用者及請求完成驗證。Provider adapter 只轉換模型協定，不另起規劃迴圈。現有直接呼叫 next_action 的 prototype 會留作明確標記的基準路徑；正式任務切換後不得兩條路線同時操作。

ActionGateway 是唯一副作用出口。它重新驗證 Action 型別、工具版本、目前 task／observation revision、精確授權 payload hash、資源 scope、lease 與 fencing token。GUI、瀏覽器、未來的檔案／受管終端／遠端 worker 都需要相同入口；缺少實作的能力預設拒絕。

## 停止與結果不明

停止先在 transaction 中改變任務狀態並撤銷 lease、未消耗 approval，再通知 driver 中斷並放開已按下的按鍵。此路徑不等待模型回覆。每個執行器在下一個輸入事件之前檢查取消狀態；失效 lease 的舊 worker 不得繼續發送。

執行外部動作之前，先持久記錄 action 已送出。若程序在送出與結果回寫之間崩潰，重啟後該動作標為結果不明，先重新觀察或驗證，不自動重播。同一 action ID／idempotency key 不得造成第二次輸入；GUI 點擊本身沒有可保證的外部 exactly-once 語意。

`done` 只提出完成候選。Verifier 必須讀回獨立 Evidence、核對所有使用者定義的成功條件與 revision，才能提交 SUCCEEDED；模型文字或多個模型互相同意均不足以提交成功。

## 輸入隔離

預設瀏覽器使用 headless Chromium 自己的滑鼠、鍵盤與頁面焦點。紫色游標來自執行器已送達的真實位置；監看串流讀取中間畫面，不改寫規劃 observation。使用者主動選擇手動接手時才開可見瀏覽器視窗。

原生共用桌面的 Cua 候選尚未通過互不干擾驗收。任何可能啟用／還原前景焦點或移動 OS 游標的路徑不進入產品執行。需要前景語意的應用程式，移至代理專用 VM／遠端互動工作階段；具體 Windows 節點仍待環境與驗收。

## 驗收邊界

契約、SQLite transaction、租約與故障測試只能證明所覆蓋的核心行為。真實模型能力、原生應用程式共存、Windows 支援及發布 Gate 必須各自取得證據；不因建立了對應模組就標成完成。
