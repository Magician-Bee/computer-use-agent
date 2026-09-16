# ComputerUSE 執行計畫

更新：2026-09-11。正式範圍依 [使用者完整規劃 v1.0](docs/plans/Computer_Use_Agent_Full_Plan_v1.0.md)，另包含下方使用者最新的背景獨立輸入修訂。原始計畫 126 項均保留；[逐項程式碼盤點](docs/plan-audit.md) 是目前實作對照，[驗收帳](ACCEPTANCE.md) 是測試與 Gate 狀態。這份文件不宣稱產品已達 v0.1 或 v1.0 的驗收標準。

## 目前基線

現有產品為 Python/FastAPI + TypeScript/React 的本機原型，具模型接頭、OCR／可選偵測、真實隔離瀏覽器、有限步數執行與最小控制台。正式工作台仍使用記憶體 legacy Run，尚未切換 Hermes／持久核心。[共用核心候選](docs/core-storage.md)與[候選 Gateway](docs/core-gateway.md)已有版本化契約、SQLite journal、Policy 與輸入前 guard；新增 [E28：Hermes 模型候選鏈路](docs/hermes-model-runtime.md)，包含 Ollama JSON adapter、LocalModelServer、ComputerTools、父程序 VerifierRegistry 與真 Hermes 四工具迴圈。真模型連線已存在，完整任務尚未成功；一般任務驗證與正式 UI 接入仍缺。既有 macOS 全域輸入原型不符合使用者最新共同工作需求。

126 項分類為 **已存在 1、部分存在 88、缺失 31、不適用 6**；最後 6 項僅是桌面 v1.0 之外的 P20 選配。分類是盤點，不是完成比例；**G0–G6 全部未通過，G7 未啟動**。

[環境報告](environment-report.json) 發現 Hermes 0.16.0（commit `646cd1b43e89920bb283dc11f38463204bcac9db`）與三個本機 Ollama 模型；隔離 bridge 已接候選 TaskStore／Policy／Gateway，腳本真動作與真 Qwen 失敗證據分列，尚未接管正式工作台。使用者目前沒有可用 Windows 測試機。這是 Windows UIA、記事本及遠端驗收的真實外部阻塞，不是刪除 Windows 範圍的理由。

v1 Qwen 原 profile（local-only evidence; not included: `artifacts/hermes-model-profile-qwen-v1/qwen3-vl-2b-profile/report.json`） 為 **0/1**：18 次 planner 推理嘗試、0 admitted action、429.067 秒。v2 首輪兩案（local-only evidence; not included: `artifacts/hermes-model-profile-v2/summary.json`）各完成一次實際 GLM-OCR transform，卻因 bootstrap validator 不相容在推理前中止，應計 **2 次 preflight failure、0 planner inference**，不是模型品質 0/2。nullable validator 修正後的 v2-run2 真實 profile 重測（local-only evidence; not included: `artifacts/hermes-model-profile-v2-run2/summary.json`）另行計分，結果為 **0/2**；原啟動失敗、舊 legacy 0/6 矩陣（local-only evidence; not included: `artifacts/local-model-matrix-grounded.json`）及腳本證據全部保留。

v2-run2：Qwen 完成 5 次真動作、6 次 planner 呼叫，422.792 秒逾時；原 oracle 5/6 符合且已送出，但座位為 aisle 而非 window，仍未成功。第 6 次提出 finish，未及處理驗證回饋。MiniCPM 2 次呼叫、0 動作、67.52 秒；先 finish 遭六項全部否決，再回傳文字，Hermes completed=true 仍不能使 Task 成功。兩案 Task 皆 CANCELLED，planner images=0、context=64000 已核對，所屬資源清理完成、來源 hash 不變。這不是新的六案矩陣，inventory／supplier 尚未在此版本計分。

## 必要使用者修訂：P03.A01 背景獨立輸入

此修訂來自原規劃之後的使用者明確要求，**不冒充原始 126 項**：

- Agent 有自己的游標、鍵盤與目標工作階段。工作時不得移動使用者的實體游標、接管鍵盤或搶走使用者目前應用程式的焦點。
- Agent 可見指標以連續路徑移動至目標，鍵盤輸入與動作按順序發生；顯示必須對應實際下發事件及目標結果。只畫一段游標動畫不算執行。
- 背景輸入不支援的 OS／應用程式組合應明確不可用；不得默默退回全域 PyAutoGUI、啟用搶焦點模式，或宣稱所有程式都支援背景操作。
- 獨立 browser context、可受控應用程式背景介面、隔離桌面／VM 或已配對遠端互動工作階段可分別評估；每條路線要測輸入隔離、資料範圍、停止與可見真實結果。
- 驗收須同時讓使用者在另一測試應用程式移動實體游標及輸入文字；記錄前景 app、游標位置、兩邊實際收到的輸入，確認沒有競爭／錯送。測試僅在約定的可丟棄環境進行。

瀏覽器局部新增 [10 個 headless 指標測試](tests/test_browser_motion.py)及實際串流像素證據（local-only evidence; not included: `artifacts/browser-live-preview-proof/report.json`）：連續 page.mouse 事件、真實 click 與獨立監看畫面。這不涵蓋一般桌面／Windows，亦未完成双方同時鍵盤輸入的 BG-01–BG-06。

目前 [server/motion.py](server/motion.py) 的軌跡 helper 及舊全域輸入成功紀錄均不授予此修訂通過。新 [Cua 候選接頭](server/cua_driver.py) 與 [背景能力閘](server/background.py) 保持 available=false；沒有通過互不干擾與連續 Agent 指標驗收，也不回退全域輸入。完成後需以 [ACCEPTANCE](ACCEPTANCE.md) 的新增測試核對。

## 必須維持的架構

使用者 2026-09-11 新增測試原則：所有實際網站錯誤須追查根因並修正共通感知、定位、輸入、決策或驗證能力；不得為取得通過而寫入某站按鈕／座標／點擊步驟，或在重測 prompt 教模型該站怎麼操作。新測試從 Google 搜尋臺灣博碩士論文知識加值系統，再在站內搜尋機械手臂論文；不預先提供目標網址。保留原失敗、實際畫面與模型動作，區分環境阻擋、程式故障、模型錯誤及獨立查核結果。工作台的完成文字不作為唯一通過依據。

依已採用的 [ADR 0001](docs/architecture.md)，一個 Supervisor／TaskStore／EventStore，一個 Hermes 主要決策迴圈，一個 Policy + Approval + ResourceLease + ActionGateway 執行出口。UI 只投影狀態；外部框架是內部能力來源。除非有實測缺口與 ADR，不把現有自寫 Run 繼續擴大成第二個正式 orchestrator。

版本化 Task、Step、ActionEnvelope、ObservationRef、Evidence、Approval、ResourceLease、Event、Checkpoint 與 ToolManifest 已於候選核心建立，Action 沿用既有定義。候選 Gateway 已把任務／觀察版本、範圍、精確核準、租約、持久 journal 與輸入前 guard 接至真實 headless driver；目前 SQLite schema v4 保留 policy digest／checkpoint 核對，從 v2／v3 以新增資料表的遷移保留任務、政策與事件。父程序模型歷史也存在同一份 TaskStore。下一步是切換唯一正式執行出口；不是增設第二份執行狀態。候選 Stop 先撤銷資料庫權限再取消及清理輸入，Planner 的 done 只提出候選；正式 Run 的獨立終態裁決尚待整合。

Hermes／核心 reviewed run2（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed-run2/report.json`）在 3.755 秒完成，由父程序獨立讀回繁中文字與一次儲存再提交 verdict，DB reopen 保留 SUCCEEDED；6 次腳本 API、2 次真動作、60 個 pointermove／983.1ms，AI 呼叫 0，Hermes 結束已清理所屬 browser。最初報告（local-only evidence; not included: `artifacts/hermes-browser-core-proof/report.json`）早於 policy pin／局部影像補強；reviewed 首輪失敗（local-only evidence; not included: `artifacts/hermes-browser-core-proof-reviewed/report.json`）保留清理後讀已關閉頁面的 harness 錯誤，修正後另建 run2。原本純 IPC 證據仍為 0 次真實電腦動作，不能與此整合混計。

回歸按版本分列：較早 509 passed／1 skipped 與後加 2 個 focused tests 保留為舊紀錄；[E28](docs/hermes-model-runtime.md) 的 v1 完整 suite 為 **699 passed、1 skipped、3 warnings，92.40 秒**，另有 **15 個 runner regression** 通過。v2 較早的 **310 focused tests／52.46 秒**只代表指定範圍；最新另跑的 v2 全量 suite 為 **801 passed、1 skipped、3 warnings，107.10 秒**。全量包含原生 OCR fixtures 與真 Hermes／fake HTTP，沒有真模型推理，live GLM 為 opt-in skip。這些數字不能相加，也不能取代真模型結果或固定 release 驗收。

Bootstrap 修正後另有 bridge＋runner **118 個局部回歸**與真 browser／真 Hermes bootstrap glue（local-only evidence; not included: `artifacts/hermes-bootstrap-real-browser-protocol-v2/protocol-proof.json`）；後者模型端仍是 fake HTTP、真模型與動作皆為 0。後加 runner 欄位測試及各次耗時在[驗收帳](ACCEPTANCE.md)分列，這些局部證據不代表重測已成功。

父程序[無損模型歷史投影](docs/model-history-views.md)已實作：只對真正釘選的 ComputerTools 結果建立可還原引用，保留改變／消失的文字、跨頁事實、原 task／限制、call/outcome 配對與最新完整觀察。原訊息、模型輸入版本及還原 manifest 先在同一份 TaskStore 提交，HTTP 接頭另核對除了 messages 以外的欄位不變，才允許一次 adapter 呼叫；沒有第二個摘要模型或 planner。目前僅限明確選用 `diagnostic_fixture`，預設完整歷史；保留額度、損壞資料與暫停／停止競態均有拒絕測試。73 個保存／遷移回歸與真 Hermes／假模型接合證據見該文件；v3 真模型重測尚無完整結果，不宣稱 token、速度或成功率改善。

[Gateway 非終態 suspend／resume](docs/core-gateway.md)已通過 7 項真 headless 暫停測試：先撤銷權限、排空輸入並釋放按鍵，保留瀏覽器；恢復只進入 REOBSERVE，須取得新 lease／revision／觀察才能輸入。這仍是核心與 driver 的局部能力；[Hermes 同一對話恢復](docs/hermes-pause-design.md)尚在設計，現有 Bridge 結束仍會關閉 worker。保存模型請求也不等於取得完整終態 conversation checkpoint。

正式 UI 切換前仍須接上 Hermes 的非終止 pause／resume、綁定 revision 的 exact approval，以及父程序成功條件／verifier 設定入口，再以 CoreSession 容器取代 app.py 的 legacy Run。CoreSession 只連接既有 Hermes 與核心服務，不另建 planner；正式接管／還權、中途修改及重啟恢復仍未完成。

加入模型歷史與核心暫停後，另跑完整 Python／benchmark suite，整合者回報 **1021 passed、1 skipped、3 warnings，136.31 秒**；此次 suite 零真模型，skip 仍是 opt-in live GLM-OCR。此為較新的自動化回歸，不覆寫上方 801 的 v2 紀錄，也不代表進行中的 v3-lossless profile 真模型評估已成功。

純文字規劃與圖像規劃分開標示。產品純視覺模式不得暗用 DOM/UIA；目前 UI 的 OCR 選項可包含 DOM，只有 benchmark 的 `--no-dom` 提供移除路徑，且該路徑尚未完成真實任務矩陣。YOLO-World 目前為可選本地接頭，沒有實測權重／UI 定位成功聲明。

## 工作批次與執行順序

| 批次 | 項目 | 具體可審查交付 | 完成前不能宣稱 |
| --- | --- | --- | --- |
| B0 盤點與契約 | P00.01–P00.06 | 保存原計畫、環境／支援矩陣、126 對照、版本化 schemas、ADR、可重建 fixtures／鎖檔 | P00 或 G0 通過 |
| B1 受控執行邊界 | P01.01–P01.06 | Supervisor、唯一 gateway、範圍授權、獨立 Stop、低權限／外送邊界與拒絕測試；沿用現有最小 UI | 任意日常帳號／桌面已安全可用 |
| B2 Hermes 整合 | P02.01–P02.06 | 沿用已建立的候選四工具鏈路與 bootstrap 修正，完成真模型原任務重測及正式 gateway／UI 接入；保留一個主迴圈 | 正式工作台已切換 Hermes、任務成功或異構端點通過 |
| B3 桌面閉環 | P03.01–P03.06 + P03.A01 | 受控背景輸入路線、連續 Agent 指標、Unicode／視窗／停止；Windows 記事本往返 10 次 | 全域游標自測等於獨立輸入／Windows 支援 |
| B4 單機自主 | P04–P09，依各項前置 | 定位模式／DPI、browser、workspace/files/managed shell、model conformance、動態 DAG、正式 verifier | G2 或通用工作成功 |
| B5 日常候選 | P10–P12 | 持久 checkpoint、副作用對帳、隱私／注入防護、正式殼／接管／中途修改 | 長任務重啟可恢復或日常成熟 |
| B6 功能整合 | P13–P17 | 語音、多 Worker／fencing、示範技能、配對遠端 Windows、資源／成本報告 | 多平台、遠端與語音已可用 |
| B7 驗收與發布 | P18–P19 | 固定 90 次能力 + 30 次強制測試，乾淨 Windows 安裝／更新／回退／移除、SBOM／文件 | v1.0 或 85.6% 成功率 |
| B8 選配研究 | P20.01–P20.06 | 獨立設備安全設計、模擬、硬體聯鎖／現場審查與受限驗收 | 真機已可操作；不阻塞桌面 v1.0 發布 |

同批或跨批的無衝突契約／fixture／文件工作可平行；依賴 Gate 尚未通過時可以準備程式，但不能宣布依賴已滿足或在未完成邊界下執行高風險真動作。無 Windows 節點期間可完成契約、Hermes 探測、背景瀏覽器、headless fixture、拒絕測試與包裝準備；Windows 及遠端實測保留真實阻塞。

## 完整階段帳

每階段六項，126 個 ID 的精確名稱、現況、證據與缺口見 [逐項對照](docs/plan-audit.md)。下表保留原規劃版本與依賴；前置是階段門檻，不是只建立同名檔案。

| 階段 | 原規劃主題／版本 | 前置 | 已存在／部分／缺失／不適用 | 階段狀態 |
| --- | --- | --- | --- | --- |
| P00 | 範圍、盤點與資料合約 · v0.1 | — | 0/6/0/0 | 未通過 |
| P01 | 執行邊界與最小控制台 · v0.1 | P00 | 1/5/0/0 | 未通過 |
| P02 | Hermes 與最小模型接入 · v0.1 | P01 | 0/6/0/0 | 未通過 |
| P03 | 桌面操作完整閉環 · v0.1 | P02 | 0/5/1/0 | 未通過 |
| P04 | 畫面理解與精準定位 · v0.3 | P03 | 0/5/1/0 | 未通過 |
| P05 | 瀏覽器操作 · v0.3 | P03 | 0/6/0/0 | 未通過 |
| P06 | 檔案、終端與程式工作 · v0.3 | P02 | 0/3/3/0 | 未通過 |
| P07 | 完整模型相容與資源管理 · v0.3 | P02 | 0/6/0/0 | 未通過 |
| P08 | 動態任務規劃與路由 · v0.3 | P04;P05;P06;P07 | 0/5/1/0 | 未通過 |
| P09 | 證據驗證與完成判定 · v0.3 | P08 | 0/6/0/0 | 未通過 |
| P10 | 長任務、中斷與復原 · v0.6 | P09 | 0/5/1/0 | 未通過 |
| P11 | 安全加固與隱私 · v0.6 | P04;P05;P06 | 0/5/1/0 | 未通過 |
| P12 | 正式桌面 UI 與接管 · v0.6 | P08;P09;P10;P11 | 0/6/0/0 | 未通過 |
| P13 | 語音互動 · v0.8 | P12 | 0/0/6/0 | 未通過 |
| P14 | 多 Worker 與排程 · v0.8 | P10;P11;P12 | 0/2/4/0 | 未通過 |
| P15 | 記憶、技能與示範學習 · v0.8 | P11;P12 | 0/1/5/0 | 未通過 |
| P16 | 遠端節點與跨平台 · v0.8 | P11;P14 | 0/1/5/0 | 未通過 |
| P17 | 效能、成本與本地體驗 · v0.8 | P07;P14;P16 | 0/6/0/0 | 未通過 |
| P18 | 完整驗收與對抗測試 · v0.9 | P13;P15;P17 | 0/5/1/0 | 未通過 |
| P19 | 安裝、維運與正式發布 · v1.0 | P18 | 0/4/2/0 | 未通過 |
| P20 | 實體設備與進階研究 · v1.x／獨立選配 | P19 | 0/0/0/6 | 未啟動（獨立選配） |

## 當前阻塞與下一個可執行項

| ID | 具體阻塞 | 後續工作 | 解鎖證據 |
| --- | --- | --- | --- |
| BL-01 | Windows 節點目前不可取得 | 完成本機可做的契約／fixture／配對與 Windows driver 準備；不以模擬代認證 | 取得明示配對、可登入互動工作階段的 Windows 受測節點及版本清單 |
| BL-02 | Hermes 候選已有真模型／真動作，正式工作台未切換，v2-run2 profile 0/2 | 保留全部原任務與啟動失敗；沿用 source pin、四工具與唯一 authority，完成正式 gateway／UI 整合及任務修正 | 原 oracle 全部通過的完整任務，以及正式工作台拒絕／取消證據 |
| BL-03 | Legacy 桌面操作共用實體輸入 | 預設不把舊路徑當合格 desktop backend；開發獨立背景輸入與真實指標同步 | P03.A01 全部新增測試通過、無全域 fallback |
| BL-04 | 正式 Planner 仍可自行寫入 completed | 把已建立的候選 ComputerTools／VerifierRegistry／Evidence／commit_verdict 接到正式完成出口，保留負面假成功 fixture | 同一正式 Run 中假成功／錯內容／無產物樣本拒絕完成 |
| BL-05 | 候選持久狀態／租約尚未接入正式原型 | 依 ADR 將已連接 Hermes／真 driver 的候選 TaskStore／Policy／ActionGateway 接入正式工作台；保留 TXN、policy pin、reopen、unknown 與取消回歸 | 正式工作台重啟／driver 崩潰與停止證據；104 個契約／store 測試及候選整合不等同已恢復一般任務 |
| BL-06 | legacy 0/6、Hermes v1 profile 0/1、v2-run2 profile 0/2 | 分列舊矩陣、真模型與 v2 preflight 失敗；用已實作的父程序無損歷史投影另測相同原 task／oracle，v3 真模型結果待本輪實測完成 | 保留全部失敗／重試／版本與 independent oracle；不能把兩個 profile 當新六案矩陣，也不能用刪掉任務事實換取速度 |

## 每批完成的紀錄規則

記錄工作 ID、檔案、實際命令、環境／模型／模式、真實輸出、仍缺的取消／失敗分支及下一步；更新本文件與驗收帳。對照原規劃第 13 節 DoD。單元／mock、固定腳本、合成協議、真實模型與發行驗收分開記錄。不得用變更成功條件、隱藏失敗、額外模型代打或現場硬編碼答案提高完成率。若來源檔案持續變更，報告保存來源 hash，舊結果不沿用為新版通過證書。
