# 完整規劃與現有儲存庫對照

盤點日期：2026-09-11。依據 [使用者原始完整規劃](plans/Computer_Use_Agent_Full_Plan_v1.0.md)（SHA-256 `2e974b94579ef078d8d0931bfbaa333b862ac4c8d64246b6e8c4026414c9005c`），完整保留 P00–P19 的 120 項必要工作與 P20 的 6 項選配。本文件是程式碼／既有證據盤點，不是 release 驗收報告。開發同時進行；新背景輸入／Hermes 程式須補上獨立測試證據才更新分類。

## 判讀

- **已存在**：該項目前要求的核心能力已有可核對實作／局部實證；依賴、完整 DoD 與階段 Gate 仍需分別驗收。
- **部分存在**：有相關實作，但原規劃要求或必要測試仍有具體缺口。
- **缺失**：盤點未找到滿足該項的產品實作／整合。鄰近的測試工具或診斷不算產品能力。
- **不適用**：只用於 P20 目前桌面 v1.0 範圍之外的獨立選配；不表示完成，未刪除後續工作。

| 分類 | 工作數 |
| --- | ---: |
| 已存在 | 1 |
| 部分存在 | 88 |
| 缺失 | 31 |
| 不適用 | 6 |
| 合計 | 126 |

目前 **G0–G6 均未通過**；G7 未啟動。單元測試、固定 demo、合成協議、局部 driver smoke、真實模型任務、完整 release suite 必須分開計數。

## 優先缺口與使用者修訂

1. 固定來源 Hermes bridge [E23] 與腳本模型／真 headless 動作整合 [E27] 之後，新增 [E28]：Ollama JSON adapter、LocalModelServer、ComputerTools 與 VerifierRegistry 已接成真 Hermes 四工具候選迴圈。v1 真 Qwen 原 profile 失敗；v2 首輪是兩次 bootstrap 啟動失敗，修正後的 v2-run2 真 profile 為 0/2。Qwen 已執行真動作但任務未通過；正式工作台仍未切換，真模型、腳本與啟動失敗不可混計。
2. [E26] 的版本化契約／ADR／SQLite TaskStore 已由 [E27] 接至候選 Policy／ActionGateway 與真 driver 輸入前 guard；目前 DB v4 保留 policy pin／checkpoint digest，從 v2／v3 新增資料表並保留任務、政策及 journal。[E29] 的父程序無損模型歷史只限明確 diagnostic fixture；[E27] 保留瀏覽器的 suspend／resume 已測，但 [Hermes 同一對話恢復](hermes-pause-design.md)尚未實作。[E28] 的父程序 VerifierRegistry 只憑實際讀回提交 evidence；工作台仍使用記憶體 Run，planner 第二次 done 仍可直接 completed。候選能力不代表正式一般任務驗收完成。
3. 使用者最新補充 **P03.A01：Agent 有獨立的游標與鍵盤，背景執行不搶使用者實體游標、輸入或前景焦點；Agent 虛擬指標必須沿連續路徑滑行，並與實際下發動作同步**。這是原始 126 項之外的必要修訂。全域 PyAutoGUI 即使平滑移動也不符合；僅播放指標動畫也不符合。
4. macOS legacy 原型只代表局部可用證據；Windows 記事本、UIA／DPI／權限及 Mac→Windows 遠端驗收仍缺可取得節點。不得用 macOS 表單或 headless browser 替代 Windows 驗收。
5. 真實模型整體基準 [E21] 是 **0/6**；保留所有失敗。不是 90 次 release 驗收，也沒有因此達成「任何模型可可靠操作」。

## 126 項對照

### P00｜範圍、盤點與資料合約

原規劃版本：v0.1；前置：—。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P00.01 | 既有環境盤點 | 部分存在 | [E01] 記錄 macOS、Hermes CLI 版本／commit、三個 Ollama 模型及 Windows 不可取得；尚缺 WSL／GPU／完整工具與互動工作階段矩陣。 |
| P00.02 | 產品範圍與非目標 | 部分存在 | 原始規劃保留 30 類任務與完整產品目標；本次 [ACCEPTANCE](../ACCEPTANCE.md) 建立對照。現有 [E18] 為 macOS 原型說明，尚無完成的 PRD／逐平台產品支援矩陣。 |
| P00.03 | 統一資料合約 | 部分存在 | [E26] 已定義 schema_version=1.0 的 Task、Step、ActionEnvelope、ObservationRef、Evidence、Approval、ResourceLease、Event、Checkpoint、ToolManifest，沿用原 Action；拒絕未知版本／欄位、錯 scope／時區／hash。尚未接入正式模組序列化，遷移與跨模組相容驗收未完成。 |
| P00.04 | 單一權威架構 | 部分存在 | [E26] ADR 0001 指定唯一 SQLite TaskStore／EventStore／journal 與 Hermes 主迴圈；候選 store 已有交易及單調 revision／fence。正式 [E03]、[E04] 仍是記憶體 Run，尚未切換唯一寫入者／gateway，不得宣稱架構整合完成。 |
| P00.05 | 可重現開發環境 | 部分存在 | [E17] 有 Python／npm 鎖檔與啟動腳本；缺 CI、格式檢查、無秘密設定模板及乾淨 Windows 安裝證據。 |
| P00.06 | 測試夾具與基線 | 部分存在 | [E14]、[E15]、[E16] 有可重建本機網頁、AppKit fixture 與 oracle；缺 Windows fixture、完整測試帳號／快照還原及統一 baseline manifest。 |

原階段驗收條件：現有能力與缺口可追溯；資料合約和測試環境可重建。

### P01｜執行邊界與最小控制台

原規劃版本：v0.1；前置：P00。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P01.01 | 本機 Supervisor | 部分存在 | [E03] lifespan 清理任務、單活躍任務鎖及 health 已有；不是獨立 Supervisor，缺工具程序樹管理、single-instance doctor 與停止通道故障隔離。 |
| P01.02 | 唯一動作閘道 | 部分存在 | [E27] 候選 ActionGateway 已連 parent-owned Policy／manifest／SQLite journal 與真 headless driver；exact scope、完整Action hash、缺核準、過期目標及跨coroutine借權拒絕有回歸。正式 [E04] 尚未切換，跨MCP／GUI／Shell唯一出口與OS沙箱仍缺。 |
| P01.03 | 最小權限模型 | 部分存在 | [E05]、[E12] 有明確上傳檔案 allowlist 及路徑身分核對；缺 app／site／account／expiry／egress 能力授權，auto 並非完整政策授權。 |
| P01.04 | 停止與取消 | 部分存在 | [E27] 候選 Stop 先撤銷DB租約／approval，再取消真driver並放開拖曳、清理browser，保留unknown；終態亦清理executor。原型 [E04] 有推理／待核準取消，但正式獨立停止控制器、全工具及≤1秒全路徑量測仍缺。 |
| P01.05 | 最小可用控制台 | 已存在 | [E10] 目標、真實狀態、逐步核準、停止、錯誤均已有；mock UI 15 群與本機固定 demo 操作已檢查。此項只認定最小 UI，不能授予 G0 或正式桌面 UI 門檻。 |
| P01.06 | 受限環境與外送控制 | 部分存在 | [E03]、[E11] loopback、Host／Origin／Fetch-Site／JSON 請求標記有測試；X-ComputerUse:1 是 CSRF 標記而非身分秘密，缺受限 OS 身分、沙箱及完整外送網路政策。 |

原階段驗收條件：任何會改變狀態的動作均受授權與停止控制；拒絕測試通過。

### P02｜Hermes 與最小模型接入

原規劃版本：v0.1；前置：P01。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P02.01 | Hermes 現況探測 | 部分存在 | [E01]、[E23] 核查固定 0.16.0 來源的 AIAgent／registry 擴充點、明確停用 session DB／fallback／環境探測；隔離 HERMES_HOME，沒有讀取或修改使用者設定／憑證。缺與正式政策／核準／任務狀態相連的完整合約。 |
| P02.02 | 執行轉接整合 | 部分存在 | [E28] 已接真 Hermes→LocalModelServer／OllamaToolAdapter→ComputerTools→SQLite／Policy／Gateway／VerifierRegistry；v2-run2 Qwen 有真動作，但兩案 profile 均失敗。[E27] 腳本證據另列，正式 Run／UI 未切換，不以候選接通宣告端到端成功。 |
| P02.03 | 單一模型真實連線 | 部分存在 | [E28] v1 真 Qwen 失敗，v2-run2 真模型 profile 重測 0/2；Qwen 局部欄位正確仍未達原 oracle。v2 首輪兩次 0 planner 啟動失敗與 [E21] legacy 矩陣分列。仍缺透過正式 gateway 成功的完整任務證據。 |
| P02.04 | 工具發現與去重 | 部分存在 | [E20]、[E28] 依目前觀察建立動態 schema，父程序只提供四個 ComputerTools；[E23] 拒絕重複／未知工具、非法順序與原生 terminal。仍缺帶版本／權限／來源的跨 MCP registry，模型不得指定 verifier 或修改工具權限。 |
| P02.05 | 輸入輸出與錯誤正規化 | 部分存在 | [E28] JSON adapter 每 completion 至多一次 planner 呼叫，轉換標準 tool_calls／buffered SSE、保留結果原文與未知用量；精確 overflow 類型、取消與格式拒絕已有局部回歸。[E23] 保留 IPC outcome／終止原因且 completion_verified=false。真 overflow 與跨 MCP 附件完整 conformance 未完成。 |
| P02.06 | Hermes 相容性隔離 | 部分存在 | [E23] 固定 commit／tree digest、私有環境／profile、預設結束清理；[E28] worker 以局部 subclass 使用 private pinned prompt hook，不改使用者 Hermes 或固定來源。此 Python audit 不是 OS 沙箱；正式整合、升級／回退與廣泛上游留存測試仍缺。 |

原階段驗收條件：一個真實模型透過唯一受控出口執行；失效不繞過權限。

### P03｜桌面操作完整閉環

原規劃版本：v0.1；前置：P02。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P03.01 | Cua 驅動接入 | 部分存在 | 新增 [Cua 候選接頭](../server/cua_driver.py) 與 [背景能力閘](../server/background.py)，固定版本／雜湊、受限 AX 操作且 available=false；尚未驗收互不干擾、連續指標或 Windows 閉環。[E05] 全域 PyAutoGUI 路徑不算合格替代；[E01] 無 Windows 節點。 |
| P03.02 | 完整輸入原語 | 部分存在 | [E02]、[E05]、[E11] 有输入原語；[E24] 的 10 個 headless browser 指標測試核對真實事件、連續路徑、雙擊／拖曳、停止釋放與 iframe。瀏覽器有限證據不涵蓋一般桌面獨立鍵盤／焦點，legacy 全域輸入仍不合格。 |
| P03.03 | 視窗與應用程式管理 | 部分存在 | [E05]、[E16] 有 legacy 開程式／PID 核對，新 Cua 候選有唯讀列舉；[桌面目標 UI](../frontend/src/DesktopTarget.tsx) 明確選 PID／視窗、顯示未就緒且不啟用視窗。缺受測一般移動／關閉管理及同名／未儲存提示政策。 |
| P03.04 | 繁中輸入與剪貼簿 | 部分存在 | [E05]、[E11]、[E16] 有 Unicode 貼上、剪貼簿備份／並發還原保護及中文 AppKit 自测；缺 Windows IME／多行／符號往返與獨立鍵盤驗收。 |
| P03.05 | 動作前後觀察 | 部分存在 | [E04]、[E08] 每動作前重觀察並拒絕 stale／遮擋；缺完整 observation ID／window ID／座標空間版本契約及 Windows 實測。 |
| P03.06 | 首個端到端任務 | 缺失 | [E16] 原生 fixture 是 macOS 單一表單，並非 Windows 記事本儲存／關閉／重開；[E01] 無可用 Windows，10 次指定閉環未執行。 |

原階段驗收條件：完成記事本建立、儲存、關閉、重開、內容核對；停止測試通過。

### P04｜畫面理解與精準定位

原規劃版本：v0.3；前置：P03。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P04.01 | 統一觀察資料 | 部分存在 | [E05]、[E07] 組合截圖、DOM／macOS AX、OCR；快照是普通 dict，缺原子一致性／版本／指定視窗私密範圍完整合約。 |
| P04.02 | Windows 結構化辨識 | 缺失 | [E05] 的 AX 擷取為 macOS；未有 Windows UIA／Win32 extractor 或狀態一致性證據；Windows 未取得 [E01]。 |
| P04.03 | 視覺定位後備 | 部分存在 | [E07] OCR 真實定位框、可選 YOLO-World、GLM 文字與低信心過濾已實作；缺廣泛 UI 定位／不確定性基線；普通物件權重不等於 UI 理解。 |
| P04.04 | DPI 與多螢幕 | 部分存在 | [E05]、[E11] 有 Retina 實體像素到主要螢幕邏輯座標測試；缺負座標、混合 DPI、100/125/150/200% 與雙螢幕。 |
| P04.05 | 失效與衝突處理 | 部分存在 | [E08]、[E11] 核對文字／狀態／座標／遮擋，有限 stale 重試；缺 UIA／Windows、跨輸入租約競爭矩陣。 |
| P04.06 | 模式與應用矩陣 | 部分存在 | [E15]、[E16] 有 browser／AppKit 框架；[E25] 新增 noDOM、vision=false、native/GLM OCR→planner→actual click 的單輪 conformance 4/8（Qwen 4/4、Mini 0/4），不是完整任務。缺完整 pure-vision/hybrid／Win32/Electron/Qt 與一般桌面矩陣。 |

原階段驗收條件：測試矩陣內定位可靠；過期畫面、錯視窗與模糊目標不盲點。

### P05｜瀏覽器操作

原規劃版本：v0.3；前置：P03。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P05.01 | 瀏覽器工作階段隔離 | 部分存在 | [E05]、[E11] Chromium 新 context 不接使用者 profile，任務分頁可辨識；缺網站／帳號授權政策與正式 profile 生命周期矩陣。 |
| P05.02 | 結構化網頁操作 | 部分存在 | [E05]、[E11]、[E13] DOM 表單、等待、分頁及 iframe 控制項已實作；iframe 靜態文字 helper 已整合，相關 driver／frame 測試 32 項通過；完整動態表單模型與 release 任務尚未通過。 |
| P05.03 | 視覺與 DOM 切換 | 部分存在 | [E05]、[E07] 支援 DOM target 與 OCR／座標操作；產品 OCR 選項仍可混入 DOM，只有 benchmark --no-dom 明確移除，缺受控純視覺產品路由與 Canvas 驗收。 |
| P05.04 | 檔案上傳下載 | 部分存在 | [E05]、[E12] upload allowlist、檔案變更拒絕、HTTP/Blob 下載完成與殘檔清理有真實 headless fixtures；缺精確目的網站授權、內容驗證器與 release 3-run 完整回執。 |
| P05.05 | 登入、付款與送出 | 部分存在 | [E04]、[E10] awaiting_input 與可見隔離瀏覽器允許人工登入；逐步 approve 只接受 bool，缺對象／內容雜湊／金額／期限綁定，auto 未精確區分外部提交。 |
| P05.06 | 瀏覽器復原 | 部分存在 | [E05]、[E08]、[E11] 有導覽拒絕、分頁處理、stale 核對與有限錯誤回饋；彈窗目前可自動 dismiss，缺提交結果不明的對帳及全面恢復矩陣。 |

原階段驗收條件：搜尋、分頁、表單、上傳下載皆可驗證；不共享未授權登入狀態。

### P06｜檔案、終端與程式工作

原規劃版本：v0.3；前置：P02。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P06.01 | 檔案操作與路徑限制 | 部分存在 | [E05]、[E12] 僅瀏覽器傳檔具 allowlist 與 symlink／替換防護；沒有一般 workspace 讀寫／複製／改名／搜尋工具或 Windows junction 測試。 |
| P06.02 | 受管 Shell 執行 | 缺失 | [E02] 沒有模型 Shell 動作；目前禁止任意 exec 是限制，不等於受限身分／網路／程序樹的 managed shell runner。 |
| P06.03 | 編碼與跨環境路徑 | 部分存在 | [E12]、[E16] 有局部 Unicode 檔案／文字測試；缺 PowerShell、WSL 映射、中文空白專案跨環境讀寫。 |
| P06.04 | 程式工作區隔離 | 缺失 | [E02]、[E04] 沒有 coding worker、分支／worktree 管理與使用者修改保護驗收。 |
| P06.05 | 啟動與介面驗證 | 部分存在 | [E03]、[E14]、[E16] 本產品 health、網頁／AppKit fixture 可啟動；缺 Agent 啟動任意授權測試專案後檢查真實 GUI 的 coding smoke flow。 |
| P06.06 | 工具策略與可逆變更 | 缺失 | [E04] 只按任務 target 選 browser/desktop；缺跨 GUI/API/程式可靠性路由、備份及可逆變更摘要。 |

原階段驗收條件：可在限定工作區修改、啟動、測試專案；不可跳出限制。

### P07｜完整模型相容與資源管理

原規劃版本：v0.3；前置：P02。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P07.01 | 模型能力登錄 | 部分存在 | [E06]、[E21]、[E28] 有本機 model metadata／最大 context 校驗、特定 renderer 相容與來源雜湊；尚無按量化／模板／後端／參數版本失效的完整能力 registry。 |
| P07.02 | 圖像與工具協議測試 | 部分存在 | [E28] 候選接頭以父程序 grammar 支援文字工具提案、保留 call/result 與 vision=false 不傳圖，真 Hermes 四工具往返已有證據；腳本／fake HTTP 與模型失敗分列。缺真實圖片對照、工具圖片回傳及全協議矩陣；buffered SSE 不是逐 token 串流證據。 |
| P07.03 | 異構端點接入 | 部分存在 | [E06] 已編寫多 provider adapter；[E28] LocalModelServer 是本機 Ollama 的 OpenAI 協定轉接，不能算第二個異構模型後端。真實非 Ollama 端點 conformance 仍缺。 |
| P07.04 | 角色路由 | 部分存在 | [E28] 真 Hermes planner 與獨立 GLM-OCR 感知已接，父程序 VerifierRegistry 負責確定性讀回，沒有第二個 planner。程式角色、完整成本計量與路由政策仍缺；GLM transform 次數不是精確推理次數。 |
| P07.05 | GPU 與上下文預算 | 部分存在 | [E28] planner 固定真實 num_ctx=64000，核對 metadata／request／ps 並記錄用量；v2 明設不截斷／不 shift，真 overflow 尚未驗證。缺 GPU admission、可調小 context、載入卸載、KV／影像預算與 OOM 排隊，不能以配置正確推論任務能力。 |
| P07.06 | 本地／雲端策略 | 部分存在 | [E07]、[E28] GLM 與 planner 接頭限定 loopback、拒絕 cloud alias、無自動雲端 fallback；LocalModelServer 使用任務權杖及來源拒絕。缺本地限定／優先／雲端許可三種完整政策與各資料範圍 egress enforcement。 |

原階段驗收條件：至少一個本地視覺端點和一個異構端點通過協議測試；本地模式不外送。

### P08｜動態任務規劃與路由

原規劃版本：v0.3；前置：P04;P05;P06;P07。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P08.01 | 目標與完成條件編譯 | 部分存在 | [E02]、[E04] 保存原始 task、追加使用者限制及步數上限；缺 Task compiler 的明確成功標準／範圍／資源／預算結構。 |
| P08.02 | 動態子任務圖 | 缺失 | [E04] 逐步 next_action 沒有帶依賴的 Step DAG、循環驗證或完成子任務不可重做規則。 |
| P08.03 | 步驟與工具路由 | 部分存在 | [E04]、[E20] 根據新觀察提出下一動作並限制 target；缺跨 browser/files/shell/worker 的能力授權路由與改環境基線。 |
| P08.04 | 重規劃與替代方案 | 部分存在 | [E04]、[E09] 失敗回饋、相同行為停機、stale 重觀察已有；缺明確錯因分類／替代策略圖與固定條件的重規劃任務驗收。 |
| P08.05 | 任務狀態機 | 部分存在 | [E03]、[E04] 有工作台狀態控制；[E26] 候選定義 QUEUED／VERIFYING／RECONCILING／INCONCLUSIVE 等狀態與交易轉換，拒絕普通 transition 寫 SUCCEEDED。尚未接正式 Run／UI／Supervisor，完整任務狀態驗收未完成。 |
| P08.06 | 長目標分段 | 部分存在 | [E04] 歷史及 user_instructions 留在單次記憶體；缺長目標分段、依賴與持久 checkpoint／交接摘要。 |

原階段驗收條件：同一目標可因環境變更重規劃，但不能擅改目標或完成標準。

### P09｜證據驗證與完成判定

原規劃版本：v0.3；前置：P08。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P09.01 | 完成分層與裁決權 | 部分存在 | [E26]、[E28] 候選 computer_finish 只提出驗證，store 核對當前 revision、外部 evidence、全部條件與未決副作用才可 SUCCEEDED；v2-run2 Mini 的 Hermes completed=true 仍留下 Task CANCELLED／benchmark false。但正式 [E04] planner 再說 done 仍可直接 completed，發布阻塞未解除。 |
| P09.02 | 確定性驗證器 | 部分存在 | [E28] 已有父程序 VerifierRegistry，將原三類 fixture oracle 對應 SuccessCriterion，再由 ComputerTools 保存 Evidence／verdict；模型不能指定 expected／selector／verifier。一般任務條件註冊、更多產物驗證與正式 Run 裁決仍未整合，不把 fixture 成功當通用驗證能力。 |
| P09.03 | 視覺與語意驗證 | 部分存在 | [E08]、[E09] 會比較可見畫面與狀態差異；只提供模型回饋／定位核對，缺有不確定終態的視覺語意完成裁決。 |
| P09.04 | 證據資料管理 | 部分存在 | [E26] 候選契約／store 保存 Evidence ID、來源、觀察版本、檢查類型與 retention 聲明；[E04]、[E15] 有事件／截圖／provenance。正式 Run 尚未使用 evidence store，也沒有實際清除／上游留存政策整合。 |
| P09.05 | 異常與負面驗證 | 部分存在 | [E28] 有 verifier 否決、驗證異常／取消不假成功、已送出動作結果不明不改報未執行的回歸；v1 真模型未完成結果保留。缺正式執行迴圈拒絕假下載、錯內容、假成功提示的全套發布阻塞測試。 |
| P09.06 | 最後交付核對 | 部分存在 | [E28] benchmark 同列持久終態、原 oracle、用量、來源與清理條件；不是正式交付報告。[E10] 仍只顯示舊 Run 完成文字／事件，缺正式逐項 success_criteria ↔ verified evidence 報告與 UI 投影。 |

原階段驗收條件：動作成功、局部結果、任務完成分開；驗證器可否決成功宣告。

### P10｜長任務、中斷與復原

原規劃版本：v0.6；前置：P09。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P10.01 | 檢查點與狀態持久化 | 部分存在 | [E26] 候選 SQLite 交易保存任務／事件／checkpoint；目前 DB v4 從 v2／v3 新增資料表並保留既有資料，policy digest 持久釘選且 checkpoint 須匹配。[E29] 模型輸入原文／可還原版本同庫保存，不是完整終態 conversation checkpoint；[E27] reviewed run2 DB 讀回 SUCCEEDED。正式 Run 仍在記憶體，Supervisor／一般 driver 重建／Hermes continuation 未完成。 |
| P10.02 | 防止副作用重複 | 部分存在 | [E26]、[E27] 候選journal在真driver前記dispatched；重送同envelope讀cached，真click oracle仍為1。reopen後unknown阻擋相同key、換ID與同資源後續動作；v2舊dispatch無policy pin不能盲授權。尚未接正式Run／一般獨立對帳，不保證GUI exactly-once。 |
| P10.03 | 重試與故障預算 | 部分存在 | [E02]、[E04]、[E06] 有界步數／重試；[E26] 候選准入核對已用動作與總時間，checkpoint 不可放寬原上限。尚未接正式總預算管理，token／成本實際用量 ledger 仍缺。 |
| P10.04 | 上下文壓縮與重建 | 部分存在 | [E29] 父程序無損投影已實作，只引用釘選工具結果中的觀察，保留改變／消失文字、跨頁事實、call/outcome 與最新完整觀察；原文／view／manifest 同庫提交並精確還原後才推理。僅 diagnostic fixture opt-in，非摘要模型；73 項保存回歸與假模型接合不證明 v3 真模型效能。正式保留政策、真 overflow、Hermes continuation 及證據／核準／未知副作用恢復仍缺。 |
| P10.05 | 卡住偵測與工具復原 | 部分存在 | [E04]、[E09]、[E11] 有無進展／重复操作偵測與模型取消；缺工具健康 watchdog、進程重啟與崩潰後安全恢復。 |
| P10.06 | 可逆補償與狀態對帳 | 缺失 | [E04] 沒有可逆補償／未知狀態對帳流程，stop 不代表回滾。 |

原階段驗收條件：崩潰、上下文壓縮及結果不明可恢復；不重複外部副作用。

### P11｜安全加固與隱私

原規劃版本：v0.6；前置：P04;P05;P06。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P11.01 | 間接提示注入防護 | 部分存在 | [E06] 提示把頁面內容標記為資料，Action schema 拒絕任意程式；缺政策強制與惡意頁面／工具輸出端到端注入套件。 |
| P11.02 | 路徑、網路與工具隔離 | 部分存在 | [E03]、[E05]、[E12] 有loopback／傳檔路徑防護；[E27] 候選明確website_origin在啟動前設定，嚴格模式拒絕跨源、全部3xx／WS／串流，轉送上限8MiB／逾時15秒。未指定origin不啟用；DNS／WebRTC／背景服務／OS及跨工具外送仍未認證。 |
| P11.03 | 憑證與精確核準 | 部分存在 | [E26]、[E27] 候選 exact-payload Approval／scope／TTL／principal、單次消耗及撤銷已接Policy／真browser；DB pin拒絕同policy ID跨重啟放寬。原型key留記憶體／錯誤遮罩，正式approve仍為bool，缺身分認證、正式Policy與OS secret store整合。 |
| P11.04 | 最小化日誌與畫面留存 | 部分存在 | [E03]、[E04]、[E10] 截圖於記憶體、API no-store、key 不匯出；legacy 事件仍含操作文字、下載留磁碟。[E29] 原始模型歷史保留僅接受明確 diagnostic fixture，額度重開不可變、有界解壓且稽核事件不含原文。正式 UI 未啟用，仍缺一般使用者 retention／刪除政策及完整 Hermes/Cua 上游快取稽核。 |
| P11.05 | 工具與供應鏈管理 | 部分存在 | [E17] 主依賴有鎖檔；[E27] 候選browser manifest保存版本／來源／實作digest，註冊與執行前核對。缺跨工具registry、SBOM／NOTICE及能力升級再驗流程，YOLO權重仍待實測。 |
| P11.06 | Windows 權限與特殊介面 | 缺失 | [E01]、[E05] 無可用 Windows 互動桌面，缺 UAC／鎖屏／管理員／RDP 實測；未實作不等於拒絕測試已通過。 |

原階段驗收條件：注入、越權、憑證外洩、惡意工具與快取留存測試通過。

### P12｜正式桌面 UI 與接管

原規劃版本：v0.6；前置：P08;P09;P10;P11。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P12.01 | 桌面殼與登入啟動 | 部分存在 | [E10]、[E17] 有本地瀏覽器 UI、雙擊 start.command；沒有受測 desktop shell、系統匣、登入啟動與 Supervisor 共生命週期。 |
| P12.02 | 任務板與即時事件 | 部分存在 | [E03]、[E10] 可讀真實 task/event/screenshot/history/export；[E26] 候選 EventStore 有每任務單調序號與交易一致性，[E29] 模型 view 與安全 metadata 事件同庫提交。UI 仍投影記憶體 Run，尚未接持久計畫／證據索引／新事件 store，也沒有正式模型歷史保留設定。 |
| P12.03 | 執行畫面與成本 | 部分存在 | [E10] 顯示真實截圖、模型、元素及操作；缺授權視窗擷取 enforcement、角色／資源／實際用量；無數據不編造。 |
| P12.04 | 人類接管與還權 | 部分存在 | [E27] 候選 Gateway suspend／resume 已測保留 headless browser、先撤銷再排空／釋放、Stop 優先，恢復須新 lease／revision／觀察；舊權限不復活。[E03]、[E04]、[E10] 的工作台仍走 legacy 暫停／回覆；[Hermes continuation](hermes-pause-design.md)、正式接管／還權與獨立桌面輸入未完成。 |
| P12.05 | 中途修改目標 | 部分存在 | [E03]、[E04]、[E11] revision + intervention 使舊模型決策／待核準操作失效；缺 versioned goal/plan/policy/approval 契約及跨 Worker 撤銷。 |
| P12.06 | 設定、可用性與無障礙 | 部分存在 | [E10] 繁中設定、目前模型與 OCR 狀態、可讀錯誤及 mobile390px 已檢查；缺正式 onboarding、全鍵盤／螢幕閱讀器矩陣與 OS 權限／隱私設定完整驗收。 |

原階段驗收條件：任務板反映真實事件；改目標、停止、接管、還權可端到端運作。

### P13｜語音互動

原規劃版本：v0.8；前置：P12。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P13.01 | 繁中語音辨識 | 缺失 | [E02]、[E10] 只有文字任務輸入；缺 STT adapter、按住說話、信心與可更正逐字稿。 |
| P13.02 | 語音回覆 | 缺失 | [E10] 無 TTS 播報／靜音／中止控制。 |
| P13.03 | 插話與中止 | 缺失 | [E04]、[E10] 無語音插話 controller；現有文字 stop 不等於語音插話驗收。 |
| P13.04 | 語音敏感確認 | 缺失 | [E02]、[E10] 無語音精確確認介面與誤辨識高風險測試。 |
| P13.05 | 音訊隱私 | 缺失 | [E10] 沒有麥克風／音訊管線；尚無獨立音訊保存／外送政策與測試。 |
| P13.06 | 端到端語音情境 | 缺失 | [E14] 只有網頁及原生文字 fixture，沒有噪音、繁中夾英文、插話語音情境。 |

原階段驗收條件：繁中語音下達與插話可用；誤辨識不直接執行高風險操作。

### P14｜多 Worker 與排程

原規劃版本：v0.8；前置：P10;P11;P12。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P14.01 | Worker 合約與註冊 | 缺失 | [E03]、[E04] 單次只執行一個 Run，沒有 worker registry／subgoal／能力繼承合約。 |
| P14.02 | 資源租約與互斥 | 部分存在 | [E26]、[E27] 候選資源唯一綁定／TTL／持久fencing接至真browser輸入前guard，查task／lease／觀察及coroutine身分；同driver改別名、舊lease、跨coroutine借權拒絕有測試。正式仍為 [E03] run_lock，多Worker／全工具租約續期尚缺。 |
| P14.03 | 有界平行執行 | 缺失 | [E04] 無子任務排程或可驗證相依交接，有界單任務不等於有界並行。 |
| P14.04 | 交接與子任務驗證 | 缺失 | [E02]、[E04] 無 worker handoff／證據版本／未決事項合約。 |
| P14.05 | 排程與事件觸發 | 缺失 | [E03]、[E10] 無產品內排程、時區、漏跑及去重規則。 |
| P14.06 | 全域取消與故障隔離 | 部分存在 | [E03]、[E04] 服務關閉會取消現有 Run；沒有父子 Worker 遞迴取消、孤兒回收及不明副作用對帳。 |

原階段驗收條件：可並行獨立工作；桌面互斥、撤銷與租約失效可阻擋舊 Worker。

### P15｜記憶、技能與示範學習

原規劃版本：v0.8；前置：P11;P12。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P15.01 | 記憶分層與期限 | 部分存在 | [E02]、[E04] 有單次歷史／action.memory 欄位；缺使用者偏好／環境／技能分層、來源、信心、TTL 與持久記憶。 |
| P15.02 | 技能定義與檢索 | 缺失 | [E02]、[E04] 沒有 skill manifest／registry／前置條件與驗證規則。 |
| P15.03 | 授權示範錄製 | 缺失 | [E10] 沒有使用者主動指定範圍的教學 recorder、遮罩或軌跡刪除。 |
| P15.04 | 語意切分與參數化 | 缺失 | [E04] 沒有示範語意切分與參數化 extraction。 |
| P15.05 | 沙箱重播與核準 | 缺失 | [E04]、[E14] 沒有新技能沙箱重播／核準／禁止未驗證技能的流程。 |
| P15.06 | 版本與失敗改進 | 缺失 | [E03] 沒有技能版本、回退、停用或衍生資料刪除 store。 |

原階段驗收條件：示範經參數化、驗證及核準後才成技能；可停用與刪除。

### P16｜遠端節點與跨平台

原規劃版本：v0.8；前置：P11;P14。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P16.01 | 遠端身分與配對 | 缺失 | [E03] 只有本機 loopback API，無遠端節點配對／雙向驗證／撤銷。 |
| P16.02 | 遠端能力探測 | 缺失 | [E01] 使用者回報無 Windows；沒有 remote doctor／互動工作階段能力探測。 |
| P16.03 | 遠端觀察與動作 | 缺失 | [E02]、[E05] 動作不帶遠端 device/session/observation version，沒有 remote executor。 |
| P16.04 | 斷線停止與防重播 | 缺失 | [E04] 無遠端本地租約 TTL、心跳、斷線拒絕與防重播。 |
| P16.05 | macOS/Linux 受測支援 | 部分存在 | [E05]、[E16] 有 macOS 局部原生 fixture 證據；Linux／Windows 未受測，macOS 全域模式亦未滿足獨立輸入新需求。 |
| P16.06 | 遠端使用與資料政策 | 缺失 | [E01]、[E03] 無配對 Mac→Windows 路線或資料傳輸政策；不把 SSH 可用等同 GUI 能力。 |

原階段驗收條件：至少一個受控遠端 Windows 節點通過；macOS/Linux 支援依矩陣標示。

### P17｜效能、成本與本地體驗

原規劃版本：v0.8；前置：P07;P14;P16。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P17.01 | 分段效能量測 | 部分存在 | [E15]、[E16]、[E21] 報告有任務、model call／observation 耗時與來源；缺所有階段 p50/p95、CPU/RAM/GPU 峰值及代表樣本。 |
| P17.02 | 按需觀察與影像壓縮 | 部分存在 | [E04]、[E05] 每步觀察、quick 原生 OCR 核對、固定尺寸截圖；缺事件驅動擷取、局部裁切、自適應解析度與版本化時效快取。 |
| P17.03 | 模型資源調度 | 部分存在 | [E03]、[E06] 單任務／串行推理有基本界線；缺模型載入卸載／GPU queue／記憶體預告 dashboard。 |
| P17.04 | 低資源常駐 | 部分存在 | [E03]、[E04] 無活躍 Run 時不自主呼叫模型或截圖；未完成固定 10 分鐘 CPU/RAM/網路/GPU 閒置量測。 |
| P17.05 | 成本與效能預算 | 部分存在 | [E02]、[E10] 可設定步數／generation 上限；無 token／實際計費／估價來源或使用者費用 budget dashboard。 |
| P17.06 | 批次動作與效能回歸 | 部分存在 | [E15]、[E21] 有保留失敗及 source hash 的 benchmark；無受控批次動作、分批觀察策略或固定效能回歸矩陣。 |

原階段驗收條件：提供實測效能與資源報告；優化不能削弱定位、驗證或權限。

### P18｜完整驗收與對抗測試

原規劃版本：v0.9；前置：P13;P15;P17。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P18.01 | 固定能力驗收集 | 部分存在 | [E14]、[E15] 只有 3 類本地 browser 基準與單一 AppKit fixture；尚未凍結完整 30×3 release suite。 |
| P18.02 | 強制安全與假成功測試 | 部分存在 | [E11]、[E12] 已覆蓋局部取消、schema、stale、秘密錯誤與傳檔；未完整執行 S01–S20，正式 verifier／政策／租約多項缺失。 |
| P18.03 | 故障注入與持久測試 | 部分存在 | [E11] 有 mock cancellation/provider failure 等故障測試；缺 F01–F10 系統重啟、磁碟、遠端、升級及持久 soak。 |
| P18.04 | 模型與平台矩陣 | 部分存在 | [E21] 保留兩個 Ollama planner 全部 6 次失敗；缺真實異構端點、Windows、多模式與固定重試預算可比矩陣。 |
| P18.05 | 外部基準參照 | 缺失 | [E14]、[E15] 使用自訂 fixtures，尚無 OSWorld 任務／重建／oracle 思路的逐項來源映射。 |
| P18.06 | 發布決策與阻塞追蹤 | 部分存在 | [E18]、[E21] 已揭露實際限制及失敗；本次建立 Gate/阻塞帳，缺固定90次+30安全故障完整結果，不能作發布通過決策。 |

原階段驗收條件：固定測試集與故障注入達到發布門檻；結果含全部失敗與重試。

### P19｜安裝、維運與正式發布

原規劃版本：v1.0；前置：P18。階段閘門：**未通過**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P19.01 | 安裝與初次設定 | 部分存在 | [E17]、[E18] 有 macOS 開發啟動／依賴安裝與網頁模型設定；沒有 Windows installer／乾淨標準使用者測試。 |
| P19.02 | 升級、回退與移除 | 缺失 | [E17] 無安裝更新管理、資料遷移、回退／移除及中斷注入測試。 |
| P19.03 | 安全發布與來源聲明 | 部分存在 | [E17] 有套件鎖與部分 benchmark source hashes；缺 SBOM、NOTICE、權重授權／release manifest、雜湊／簽章發行流程。 |
| P19.04 | 診斷與支援包 | 部分存在 | [E10] 可匯出 task JSON 及可讀錯誤；這含任務／動作資料而非可預覽遮罩的支援包，缺安全診斷工具。 |
| P19.05 | 操作與開發文件 | 部分存在 | [E18]、[E07]、[E10]、[E15]、[E16] 有啟動、模型、感知與局部測試文件；缺正式授權、遠端、技能、恢復、停用及新使用者完整操作文件。 |
| P19.06 | 正式發布與維護規則 | 缺失 | [E01] release_gate_status=not_passed；未完成 P00–P19、v1.0 artifacts／release notes／維護規則，不得因 API 標示 0.1.0 而稱 G1 通過。 |

原階段驗收條件：乾淨電腦安裝、升級、回退與移除可用；所有必要門檻通過。

### P20｜實體設備與進階研究

原規劃版本：v1.x／獨立選配；前置：P19。獨立選配：**未啟動／不列為桌面 v1.0 通過項**。

| ID | 原工作名稱 | 分類 | 可核對現況與未完成部分 |
| --- | --- | --- | --- |
| P20.01 | 實體設備獨立邊界 | 不適用 | 桌面 v1.0 不包含實體設備；P20 保留為獨立選配，不從未來路線刪除。未有 device safety design，現階段不提供真機使能。 |
| P20.02 | 只讀診斷與數位模擬 | 不適用 | 獨立設備／研究選配尚未啟動；缺只讀診斷／模擬 worker，不能聲稱模擬或真機驗收。 |
| P20.03 | 設備命令與硬體聯鎖 | 不適用 | 無設備 gateway、硬體急停或現場聯鎖；須獨立安全專案，不以桌面 Stop 替代。 |
| P20.04 | 受限真機驗收 | 不適用 | 未有設備、現場資格／核準及受限真機驗收；保持未啟用，不是已完成。 |
| P20.05 | 資料與模型研究 | 不適用 | 權重訓練與私人軌跡研究非目前桌面交付；需獨立同意／資料計畫，不沿用任務資料授權。 |
| P20.06 | 進階硬體與互動研究 | 不適用 | 遊戲、CAD/3D 與其他研究軌道保留 P20 選配；沒有獨立基線，不因列入規劃宣稱支援。 |

原階段驗收條件：僅在獨立安全專案、模擬與現場驗收通過後才允許受限真機實驗。

## 證據範圍與待整合回歸

- 前端：`npm --prefix frontend test` 的最後一次實測為 16 tests（1 父群 + 15 工作流群）通過、0 失敗；執行前會 build。mock API、headless Chromium，不操作原生桌面、不呼叫模型。[E10]
- 正式工作台 HTTP E2E：成功報告（local-only evidence; not included: `artifacts/workspace-live-preview-proof-run2/report.json`） 27 checks 通過；UI 啟動背景 demo，先解碼真 /view 再做兩次核準，完成與另一任務 Stop 均轉 /screenshot；零 AI、console/pageerrors，1280px／390px畫面檢查。自有服務已退出、未碰8765；首輪 harness 參數錯誤（local-only evidence; not included: `artifacts/workspace-live-preview-proof/report.json`）保留。此固定 demo 不授予 Gate。
- 原生固定驅動自測：`artifacts/native-driver-smoke-production-quartz/report.json` 為 5 動作／15.855 秒、oracle 通過、model_calls=0。它是全域輸入表單自測，不符合 P03.A01，也不是模型成功或 Windows 記事本閉環。[E16]
- 真實 browser 模型矩陣：[E21] 兩模型各 profile/inventory/supplier 共 6 次，passed=0。其他個別診斷／候選報告不能用最好一次覆蓋此矩陣。
- iframe 回歸：新增 `server/browser_text.py` 與 [E13]；首次為 6 個 helper 檢查通過、1 個整合失敗。drivers.py 已匯入並呼叫 `collect_page_text(page)`；驅動負責者回報 `pytest tests/test_browser_frames.py tests/test_drivers.py -q` **32 passed，6.59 秒**。修正包括可見跨來源／巢狀 iframe 文字，排除隱藏 iframe；不是整個 P05 門檻通過。
- Hermes 候選：[E23] 的 reviewed scripted SSE 報告（local-only evidence; not included: `artifacts/hermes-protocol-conformance-reviewed.json`）為 5 輪固定回覆／3 次 parent gateway，另有第 5 輪故意失敗的報告（local-only evidence; not included: `artifacts/hermes-protocol-fault-conformance.json`）；兩者 real_model_calls=0、real_computer_actions=0。故障報告 passed 只表示預期失敗／無 request dump／canary 未留存，不是任務成功。[假子程序回歸](../tests/test_hermes_bridge.py)首輪抓到 stop/result 競態，修正後 **41 passed**；沒有因此授予 P02 或 G1 通過。
- Browser 指標與串流：[E24] 的實際 headless 報告有 13 個 JPEG frame、11 個不同 frame、10 個紫色指標像素位置，真 click 改變 fixture oracle；決策 observation hash 不變。瀏覽器滑鼠事件與 preview 是真實 executor 結果，model_calls=0；仍未驗收使用者在其他應用同時輸入、一般桌面或 Windows 的 P03.A01。
- 純文字 planner 的 OCR 單輪：[E25] 8 輪全部 DOM=0／planner images=0；Qwen 4/4、Mini 0/4，後者回傳 scroll 被 fixture 拒絕且未點擊。GLM 文字仍標記 incomplete，定位框由 Apple Vision 供應。這是有界定位動作 conformance，不能覆蓋 [E21] 0/6 或後續完整 profile 0/2 的失敗。
- 共用核心候選：[E26] 的 [契約測試](../tests/test_core_contracts.py)與 [store 測試](../tests/test_task_store.py)，較早版本整合者回報 **56 + 48 = 104 passed**。涵蓋 transaction／revision 衝突、reopen、單次准入／unknown拒絕、停止撤銷、policy pin及checkpoint digest核對。該版 DB v3 保留資料升級 v2，舊 dispatch 無 pin 拒絕盲授權；目前 schema v4 的 v2／v3 additive 遷移另有 [E29] 證據。正式工作台重啟恢復與真 driver 崩潰對帳仍未驗收。
- 共用執行候選：[E27] 的 [policy](../tests/test_core_policy.py) **43**、[gateway](../tests/test_core_gateway.py) **24**、[driver authority](../tests/test_browser_authority.py) **11** 測試通過。真headless回歸涵蓋cached去重、缺核準／stale target拒絕、先撤銷再取消／釋放、跨coroutine拒絕、重啟policy不能放寬與v2遷移。[嚴格origin](../tests/test_browser_origin.py)另有16個局部測試，跨來源HTTP／iframe／redirect／WebSocket禁止端TCP與HTTP接觸為0；後加2個gateway／origin接口測試另行通過。限制與未涵蓋網路通道見[E27]，不是OS沙箱證書。
- Hermes／核心整合：[E27] reviewed run2（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed-run2/report.json`）為 **3.755秒**、6次腳本API／5次工具、2次真實browser動作、60 pointermove／983.1ms；獨立繁中逐字相符／saveCount=1、DB reopen=SUCCEEDED，Hermes結束所屬browser已清理。real_model_calls=0、Hermes completion_verified=false。最初報告（local-only evidence; not included: `artifacts/hermes-browser-core-proof/report.json`）早於policy pin／局部影像補強；reviewed首輪失敗（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed/report.json`）保留passed=false（清理後讀已關閉頁面的harness錯誤），修正擷取時序後另建run2，沒有覆寫失敗。
- Hermes 真模型 v1：[E28] 的 Qwen 原 profile 報告（local-only evidence; not included: `artifacts/hermes-model-profile-qwen-v1/qwen3-vl-2b-profile/report.json`）為 **0/1**，18 次 planner 推理嘗試、0 admitted action、429.067 秒；DOM＋GLM-OCR、planner images=0，終態 CANCELLED、原 oracle 未通過。這是候選真模型失敗，不覆寫 [E21] legacy 0/6，也不與 [E23]／[E27] 腳本證據混計。
- Hermes v2 啟動失敗：兩案報告（local-only evidence; not included: `artifacts/hermes-model-profile-v2/summary.json`）記錄 Qwen 與 MiniCPM 各完成一次實際 GLM-OCR transform，但 bootstrap observation validator 與產生的觀察不相容，均在任何 planner 推理前中止；**2 次 preflight failure、0 planner inference、0 admitted action**。不是模型品質 0/2；GLM transport 精確呼叫數／tokens 未計量。原失敗保留；nullable validator 修正後的真模型結果另列下一項。
- Hermes v2-run2 真模型：最新 profile 報告（local-only evidence; not included: `artifacts/hermes-model-profile-v2-run2/summary.json`）為 **0/2**。Qwen：5 次真動作、6 次 planner 呼叫、422.792 秒逾時；原 oracle 5/6，已送出但 seat=aisle 而非 window，第 6 次 finish 未及處理驗證回饋。MiniCPM：2 次呼叫、0 動作、67.52 秒；finish 六項全否決後僅回文字，Hermes completed=true 仍不能使 Task 成功。兩案 Task CANCELLED、planner images=0、context=64000 已核對，所屬資源清理與來源 hash 穩定。這是兩案 profile，不是新六案矩陣；此版本 inventory／supplier 尚無成績。
- Bootstrap 修正後的局部證據：[E28] 整合者回報 bridge＋runner **118 passed，10.67 秒**；後加 phase/logging 欄位的 runner **16 passed，9.70 秒**另列。真 headless＋假 enricher 完整 glue 測試 **1 passed，3.49 秒**，其協定報告（local-only evidence; not included: `artifacts/hermes-bootstrap-real-browser-protocol-v2/protocol-proof.json`）為真 Hermes、0 真實模型／1 次 fake HTTP／0 動作，固定 system 1727 字元。這些證據不取代兩案真模型重測，也不是新全量 suite。
- 較早完整 Python／benchmark suite 的 **509 passed、1 skipped、3 warnings，69.91 秒**與後加 2 個 focused tests 保留為舊版本紀錄；沒有重跑該版全套 511。後續 [E28] v1 完整 suite 為 **699 passed、1 skipped、3 warnings，92.40 秒**，另有 **15 個 runner regression** 通過。v2 較早的 **310 focused tests／52.46 秒**只代表該次指定範圍；不同版本／範圍不得相加。
- 最新 v2 全量回歸：整合者執行 `.venv/bin/python -m pytest tests benchmarks/test_benchmarks.py -q --tb=short`，結果 **801 passed、1 skipped、3 warnings，107.10 秒**。包含 native OCR fixtures 與真 Hermes／fake HTTP；無真模型推理，skip 為 opt-in live GLM-OCR。這是自動化回歸，不覆蓋 v2-run2 真任務 0/2，也不等同固定 release suite；所有 Gate 不變。
- 無損模型歷史：[E29] 的純 codec **74 項**與 ModelViewSession **73 項／3.77 秒**回歸分列。後者涵蓋原始工具字串逐位元組還原、同庫提交後才呼叫 adapter、任務範圍／額度、第二連線 pause／Stop CAS、journal 回滾、損壞／解壓上限拒絕，以及 v3→v4 遷移保留 task／events／policy。真 Hermes／headless／假模型 v3 接合報告（local-only evidence; not included: `artifacts/hermes-lossless-real-browser-protocol-v3/protocol-proof.json`）有兩次假回覆、零真模型／零輸入，新增合成診斷文字後實際收到引用且可還原；不是真任務成功或效能結果。v3-lossless 真模型重測進行中，本列不填推測成績。
- 非終態核心暫停：[E27] 的 [7 項真 headless 測試](../tests/test_core_gateway_suspend.py)與 gateway／感知／四工具／store 暫停局部合跑 **84 passed，37.80 秒**。確認保留 browser、權限先撤銷、清理不隨呼叫方取消、恢復必須重觀察、Stop 優先，沒有真模型或共用桌面輸入。[Hermes continuation 設計](hermes-pause-design.md)仍未實作，這些測試不能代表同一 worker／conversation 已可暫停恢復。
- 新一輪全量回歸：加入無損歷史與核心暫停後，整合者回報相同完整 Python／benchmark suite **1021 passed、1 skipped、3 warnings，136.31 秒**。suite 零真模型，skip 為 opt-in live GLM-OCR；保留較早 801 與其他版本紀錄，不相加，不替代進行中的 v3 真模型結果或任何 release Gate。
- 本次文件更新只讀取既有報告，未新增模型、native、headed 或 Windows 操作。程式來源與報告可作局部證據，沒有執行輸出的測試只列「有測試」，不列「通過」。

## 證據索引

缺失判斷來自對 source tree、schemas、Run/API/driver/provider/前端及 tests/benchmarks 的讀取；不是以檔名猜測外部環境。只讀盤點使用 `rg --files`（排除依賴／生成檔）及 `rg -n` 搜尋類別／函式入口，再讀取實作。外部專案是否已整合以本 repo 契約／實測為準；原規劃的官方來源清單不算安裝證明。

| 代號 | 檔案／證據範圍 |
| --- | --- |
| [E01] | 環境、Hermes 發現、Windows 可用性；由 root 執行只讀盤點 |
| [E02] | 目前模型、動作、任務請求與 approve bool 型別 |
| [E03] | 本機 API、記憶體狀態、單活躍任務、來源邊界與控制 |
| [E04] | 目前自寫 Run 主迴圈、重新觀察、取消、重試與完成判定 |
| [E05] | 瀏覽器與 legacy 全域桌面驅動；不是正式 Cua 支援證書 |
| [E06] | 模型協定、Action parse、限制與錯誤处理 |
| [E07] | OCR／GLM／YOLO 設定、程式與真實合成測試範圍 |
| [E08] | 過期元素、畫面與遮擋核對 |
| [E09] | 動作後可見狀態、進度 fingerprint；不裁決任務成功 |
| [E10] | 實際前端驗證、source links、15 群 mock API 測試與限制 |
| [E11] | API／provider／driver／agent 單元與局部整合測試來源；存在不等於全部已執行 |
| [E12] | headless 傳檔、symlink／替換、殘檔取消測試 |
| [E13] | 可見跨來源／nested iframe 靜態文字與隱藏 iframe 回歸 |
| [E14] | 3 類可丟棄 browser fixture 及獨立 state oracle |
| [E15] | 真實本地模型 browser benchmark、no-dom／vision 與 provenance 定義 |
| [E16] | AppKit fixture、正式全域／診斷輸入之區分與實測限制 |
| [E17] | 啟動入口；另參 uv.lock、frontend/package-lock.json、pyproject.toml |
| [E18] | 現有原型使用／支援限制；本次完整規劃優先於舊原型範圍 |
| [E20] | 從觀察建立操作 schema；沒有執行權／終態裁決權 |
| [E21] | 既有完整兩模型×三任務報告，6 次均未通過；不是 release90次 |
| [E22] | 連續 trajectory 純 helper；不能單憑檔案存在推論背景輸入可用 |
| [E23] | 固定 Hermes bridge、worker、source pin；41 假子程序回歸與 scripted SSE／故障協議報告 |
| [E24] | 實際 headless browser 指標／multipart JPEG 監看證據；不代表一般桌面共同工作 |
| [E25] | noDOM／文字 planner／OCR 的 8 輪定位輸入 conformance，4/8；不是完整任務 |
| [E26] | [版本契約](../server/core/contracts.py)、[候選 TaskStore](../server/core/store.py)、[契約測試](../tests/test_core_contracts.py)、[store 測試](../tests/test_task_store.py)、[ADR 0001](architecture.md)與[儲存說明](core-storage.md)；保留較早 104 局部測試與 DB v3 證據，目前 DB v4／v2與v3保留遷移／policy pin／checkpoint digest，正式 Run 尚未接入 |
| [E27] | [候選 Gateway 說明](core-gateway.md)、[整合腳本](../scripts/diagnose_hermes_gateway.py)、reviewed run2（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed-run2/report.json`）、原首輪（local-only evidence; not included: `artifacts/hermes-browser-core-proof/report.json`）及reviewed失敗（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed/report.json`）；真 Hermes／SQLite／Policy／headless driver，腳本模型、2 真動作、AI 呼叫 0；保留 policy43／gateway24／authority11 局部證據。新增 [7 項 suspend 測試](../tests/test_core_gateway_suspend.py)驗證保留 browser／還權邊界；[Hermes continuation](hermes-pause-design.md)及正式 UI 未接入 |
| [E28] | [Hermes 模型候選鏈路](hermes-model-runtime.md)：Ollama JSON adapter、LocalModelServer、ComputerTools、VerifierRegistry 與真 Hermes 四工具迴圈；v1 原任務失敗、v2 bootstrap 啟動失敗與 v2-run2 真 profile 0/2（local-only evidence; not included: `artifacts/hermes-model-profile-v2-run2/summary.json`）分列。正式 Run／UI 尚未切換，不授予 Gate |
| [E29] | [無損模型歷史說明](model-history-views.md)、[codec](../server/context_projection.py)、[同庫 ModelViewSession](../server/core/model_views.py)、[73 項保存／遷移測試](../tests/test_model_views.py)及真 Hermes／假模型接合（local-only evidence; not included: `artifacts/hermes-lossless-real-browser-protocol-v3/protocol-proof.json`）；父程序釘選、原文／view／manifest 先提交後推理、精確還原與 bounded readback。僅 diagnostic fixture opt-in；不等於正式保留政策、完整 conversation checkpoint 或真模型效能 |

[E01]: ../environment-report.json
[E02]: ../server/schemas.py
[E03]: ../server/app.py
[E04]: ../server/agent.py
[E05]: ../server/drivers.py
[E06]: ../server/providers.py
[E07]: perception.md
[E08]: ../server/grounding.py
[E09]: ../server/feedback.py
[E10]: ui-verification.md
[E11]: ../tests/
[E12]: ../tests/test_driver_files.py
[E13]: ../tests/test_browser_frames.py
[E14]: ../benchmarks/cases.py
[E15]: ../benchmarks/README.md
[E16]: ../benchmarks/native.md
[E17]: ../start.sh
[E18]: ../README.md
[E20]: ../server/action_space.py
[E21]: ../artifacts/local-model-matrix-grounded.json
[E22]: ../server/motion.py

[E23]: ../server/hermes_bridge.py
[E24]: ../artifacts/browser-live-preview-proof/report.json
[E25]: ../artifacts/perception-action-conformance.json

[E26]: core-storage.md
[E27]: core-gateway.md
[E28]: hermes-model-runtime.md
[E29]: model-history-views.md
