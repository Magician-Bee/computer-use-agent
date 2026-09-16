# Hermes 同一對話的暫停／恢復設計邊界

日期：2026-09-11。狀態：**設計候選，尚未實作 Hermes 暫停／恢復，非 release Gate**。

本文件來自唯讀原始碼審查，沒有模型推理、桌面操作或動態 Hermes 恢復驗收。現有 `ActionGateway.suspend()`／`resume()` 已提供保留隔離瀏覽器的執行權限邊界；這不代表 `HermesBridge` 已能保留並恢復同一段 conversation。

下一輪的最小目標：保留同一個 worker／AIAgent，以協作式中斷取得完整 history；父程序排空輸入與推理後，只有在重新取得權限及真實新觀察時才允許下一個 turn。Hermes 仍是唯一 planner，不新增第二個規劃迴圈。

## 現況與缺口

### 目前的取消是終止，不是暫停

`HermesBridge.run()` 的 `finally` 必定呼叫 `close()`。後者取消進行中的 gateway callback、執行 `cancel_gateway`，然後終止 worker。benchmark 傳入的 callback 會呼叫 `gateway.stop()` 並關閉瀏覽器。因此不能取消 `bridge.run()`，再把它視為可恢復的 pause。

來源：[server/hermes_bridge.py:182](../server/hermes_bridge.py#L182)、[server/hermes_bridge.py:195](../server/hermes_bridge.py#L195)、[scripts/benchmark_hermes.py:220](../scripts/benchmark_hermes.py#L220)、[scripts/benchmark_hermes.py:256](../scripts/benchmark_hermes.py#L256)。目前 Bridge 也限制每個實例只能啟動一次 conversation：[server/hermes_bridge.py:82](../server/hermes_bridge.py#L82)。

**必要修改：** 將非終止的 pause／resume 與永久 close 分開。暫停不得觸發現有 terminal cleanup；Stop、致命錯誤和最終關閉仍使用既有清理責任。benchmark 的單次執行生命週期不能直接充當可恢復任務的 supervisor。

### Hermes 能返回 history，但目前 worker 不輸出它

Pinned Hermes 的 `interrupt()` 可由另一執行緒呼叫；conversation loop 會檢查 interrupt，正常返回的結果包含 `messages`。目前 worker 只輸出 final、usage、interrupted 等欄位，丟掉完整 history。

來源：run_agent.py:2333（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/run_agent.py`）、conversation_loop.py:567（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/conversation_loop.py`）、turn_finalizer.py:325（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/turn_finalizer.py`）、[scripts/hermes_worker.py:219](../scripts/hermes_worker.py#L219)。

**必要修改：** worker 返回獨立的 `paused` 訊息，包含完整 `messages`、conversation identity、turn generation、最後工具 sequence、history hash 與訊息計數。父程序驗證並保存後才 ACK。只有一次已完成的 `run_conversation()` 所返回的穩定資料能作為這次協作式暫停的 checkpoint；不可在另一執行緒任意讀取仍在變動的內部 messages，便宣稱 history 完整。

`retain_profile` 不是 checkpoint。現有配置關閉 session snapshots，worker 關閉 session DB、trajectories 與 checkpoints：[server/hermes_bridge.py:110](../server/hermes_bridge.py#L110)、[scripts/hermes_worker.py:206](../scripts/hermes_worker.py#L206)。父程序現有 model view 保存的是送出推理前的 request，不能假定其中已有最新模型回覆或尚未回傳的工具結果：[server/core/model_views.py:141](../server/core/model_views.py#L141)。

完整 history 可能超過目前 1 MiB 的單行 IPC 限制：[server/hermes_bridge.py:119](../server/hermes_bridge.py#L119)。需設計有界分塊，或具有完整性驗證、存取範圍與保留政策的引用。超過容量須明確拒絕 checkpoint，不能默默截斷或摘要後稱為完整 history。這個設計不授權默認保存使用者的完整敏感對話。

### 控制訊息不能直接插入現有 stdin

目前 worker 的工具 handler 持有 lock，直接 `sys.stdin.readline()` 等待同一 sequence 的結果。若父程序在此時插入 pause JSON，它會被誤認為工具 reply 並導致 sequence mismatch：[scripts/hermes_worker.py:181](../scripts/hermes_worker.py#L181)。

**必要修改：** 只有一個 stdin reader，依訊息種類分流工具結果與 pause／resume／stop；各等待者透過佇列或其他明確同步機制取得自己的訊息。寫入端也需序列化。控制命令帶 conversation identity、generation 與 control ID，ACK 必須精確對應；舊 generation 的訊息不得推動新 turn。

Pause 控制執行緒呼叫 `agent.interrupt()`；正在等待父程序 reply 的工具不能只靠這個旗標解除阻塞。父程序須在 durable pause 與輸入排空後，回覆該 sequence 的真實結果：已知 receipt、已確認未執行，或 `outcome_unknown`。不得把可能已發生的輸入一律寫成 `executed:false`。Gateway suspend 取消 child task 時，Bridge 要能區分這是受控暫停，避免 child `CancelledError` 直接傳出並執行 `run()` 的 terminal `finally`。

Hermes 在收到 interrupt 後會為未開始的後續工具補上 skipped tool result，這些是中斷記錄，不能解讀為輸入成功：tool_executor.py:770（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/tool_executor.py`）。工具呼叫與結果的配對必須完整保留。

## 父程序的順序與協定狀態

下表是**建議的 supervisor／IPC 狀態**，不是新增的 TaskStore 狀態，也不是已實作 API。

| 協定階段 | durable task／輸入權限 | worker 與模型處理 | 完成條件 |
| --- | --- | --- | --- |
| RUNNING | 當前 revision、lease、fence 與 observation 才能 dispatch | 唯一 active turn | 收到 pause 或 Stop |
| PAUSING | 先提交 Gateway pause，撤銷 lease／approval，進行中的 dispatch 標為 unknown | 關閉新 inference admission；發送 interrupt；排空工具與模型請求 | Gateway 清理、舊 turn、舊 inference 均已排空 |
| PAUSED | 保留瀏覽器；不允許輸入 | 保存完整 history 並 ACK；不得自行開啟新 turn | 父程序接到有效的 resume，且未知效果已對帳 |
| RESUMING | `gateway.resume()` 僅進入 REOBSERVE；取得新 lease，再進入 RUNNING 並完成新觀察 | 更新 facade 畫面／動態 schema；檢查 generation 與累積預算 | 新權限、新畫面及 planner 唯一性全部成立後才發送 resume |
| STOPPING／STOPPED | Stop 撤銷權限並關閉擁有的資源 | 永久拒絕新 turn；晚到的 pause／resume ACK 無效 | terminal cleanup 排空；不得復活 |

Gateway 現有順序與恢復要求見 [server/core/gateway.py:312](../server/core/gateway.py#L312)、[server/core/gateway.py:376](../server/core/gateway.py#L376)、[server/core/gateway.py:220](../server/core/gateway.py#L220)。Store 會拒絕有未知效果的 resume：[server/core/store.py:436](../server/core/store.py#L436)。

父程序應先設置本機的 pause 意圖，使 Bridge 能辨識隨後被 Gateway 取消的 callback；接著立即透過 Gateway 提交 durable pause。這個意圖本身不能授予或撤銷 durable authority，也不能讓 worker 繼續派送。所有真正的輸入仍受 Store／Gateway 判定。

恢復要更新 ComputerTools 所持有的 lease、frame、model view 及當前 schema；只在 Gateway 取得新 frame，卻讓 facade 留著舊資料，仍會造成 stale proposal：[server/core/computer_tools.py:101](../server/core/computer_tools.py#L101)、[server/core/computer_tools.py:191](../server/core/computer_tools.py#L191)。

將完整 `messages` 作為下一次 `conversation_history`，但不要再次把原始 task 當新 user message。Pinned Hermes 每次都會 append 一個 user turn：turn_context.py:221（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/turn_context.py`）。恢復訊息必須對應實際使用者的恢復操作，記錄其來源；新觀察以明確的父程序讀取結果提供，不可冒充新的使用者指令。原始任務字串保持原樣。

## 必須封住的競態

1. **Gateway 已取消輸入，但 Bridge 將取消當成 Stop。** 需有受控 pause 分支及對應工具 reply，不能讓現有 `finally close()` 關掉瀏覽器。
2. **收到 `interrupted:true`，舊 inference 仍在執行。** Hermes 的 API daemon thread 可能超過 turn 的生命週期；中斷會關閉 request-local transport，但不是所有底層工作已退出的證據：chat_completion_helpers.py:142（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/chat_completion_helpers.py`）、chat_completion_helpers.py:528（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/chat_completion_helpers.py`）。父程序 endpoint 需新增非終止的 admission gate 與 cancel／drain 入口，等待 active request 清空後才可 resume。現有 close 是永久性的：[server/model_server.py:318](../server/model_server.py#L318)。取消 HTTP 不等於已證明 Ollama 底層推理立即停止，兩者要分別記錄。
3. **兩個 resume 或重建兩個 Bridge 產生兩個 planner。** 每 task 的 supervisor lock／generation 必須涵蓋 worker 建立、turn 開始、pause ACK、resume 與終止。現有 endpoint `_busy` 只限制同一個 endpoint，不能保護另一個新建 endpoint：[server/model_server.py:206](../server/model_server.py#L206)。上一 turn 未返回且請求未排空時，不得呼叫第二次 `run_conversation()`。
4. **Stop 與 pause／resume 同時到達。** Stop 是不可逆決定，撤銷 generation 與模型 admission；每個 await 回來後重新檢查。晚到的 checkpoint 或 ACK 可作記錄，不能轉成可恢復授權。現有 Gateway 對同 revision 的 pause／Stop 競爭已有有限處理，不代表跨程序 planner lifecycle 也已處理：[server/core/gateway.py:396](../server/core/gateway.py#L396)。
5. **舊模型輸出或未確定的工具被重送。** 舊 generation 的輸出不能取得新 frame／lease；pending effect 不得以換 action ID 的方式重播。必須先讀回及對帳，然後重新觀察、重新提出動作。
6. **暫停 history 已保存，但 facade 仍回傳舊 schema。** Resume 的 worker 啟動閘門必須放在新 observation 和 schema 更新之後，不能僅以 `REOBSERVE` 的狀態轉換作為恢復完成。

## 累積預算與推理 admission

Pinned Hermes 每次 `run_conversation()` 都建立新的 iteration budget：turn_context.py:166（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/turn_context.py`）。若每次 resume 都給原上限，使用者就能藉暫停／恢復重置總預算。父程序須持續累計已使用與未知的推理、tokens、費用及執行預算，只將剩餘額度交給下一 turn；未知 usage 不能算成零。

另外，finalizer 在 iteration budget 耗盡時可能呼叫模型產生摘要，條件本身沒有排除 interrupted：turn_finalizer.py:53（local-only evidence; not included: `.tools/hermes/646cd1b43e89920bb283dc11f38463204bcac9db/agent/turn_finalizer.py`）。所以不能只依賴 worker 的 interrupt 旗標保證「暫停後不再推理」；父程序的 admission 必須在暫停時拒絕任何新請求，包括摘要、retry 和已排程但尚未轉發的請求。現有 request preparation／disconnect cancellation 可作整合起點，但不是已完成的 pause gate：[server/model_server.py:213](../server/model_server.py#L213)、[server/model_server.py:247](../server/model_server.py#L247)。

是否將人為暫停時間計入 wall-clock deadline，須由父程序政策明訂並保存；不能因建立新 turn 就偷偷延長任務期限。

## worker 重建與驗收界線

優先實作保留 worker 的協作式暫停。安全重建 worker 是後續能力，至少要有父程序已 ACK 的完整 checkpoint、原 tool-call/result 配對、來源及配置 pin、累積預算、最後 generation，並確認舊 worker 與模型請求已排空。新 worker 只接收父程序核准的 conversation，不得自行讀取使用者 Hermes profile 或從文字摘要猜回遺失的操作。

**現有硬取消／kill 沒有完整 checkpoint 保證，不能宣稱可還原同一段完整 conversation。** 若最新 history 未成功返回或保存，須明確標記恢復不完整／不可恢復，不能把最後一次 model request 當作完整終態。

下一輪先使用真 pinned worker／Bridge、fake model endpoint 與隔離 headless fixture 驗證：模型等待中暫停、工具已知 receipt 後暫停、未知 dispatch 阻擋恢復、checkpoint ACK 與 Stop 競爭、重複 resume、舊 generation 回覆、history 超限與完整性錯誤、累積預算不可重置。這些是待寫的驗收案例；本文件不聲稱它們已通過，也不聲稱 Hermes 已支持恢復。

## 來源版本

本次核查使用專案內唯讀固定 export：Hermes commit `646cd1b43e89920bb283dc11f38463204bcac9db`，tracked tree SHA-256 `e2b7f2d5c1ea50813d59426755ce0c801cf84c9605ab35fe2be905d13d65e4d4`。pin 與驗證入口見 [server/hermes_bridge.py:22](../server/hermes_bridge.py#L22)、[server/hermes_bridge.py:31](../server/hermes_bridge.py#L31)。連結行號對應 2026-09-11 的審查快照；後續改動時須重新核對。
