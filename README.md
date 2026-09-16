# ComputerUSE

本機執行、可切換模型的電腦操作工作台。目標是讓模型完成真實、多步驟的電腦任務，包括純文字 LLM：畫面先經過 OCR、可存取性資訊與選配物件偵測，再轉成可操作的元素 ID。

最終目標與 126 項工作依照 [完整規劃](docs/plans/Computer_Use_Agent_Full_Plan_v1.0.md)。目前仍在建置與驗證：兩個小模型的三項完整任務基線是 **0/6**，所有發布 Gate 都尚未通過。進度與證據見 [實作計畫](PLAN.md)、[驗收帳](ACCEPTANCE.md) 與 [逐項對照](docs/plan-audit.md)。

**輸入必須獨立：代理有自己的游標與鍵盤狀態，不得移動使用者的實體游標、搶鍵盤焦點或借用使用者剪貼簿。** 隔離瀏覽器透過自己的 Chromium 輸入通道操作。原生桌面的背景驅動仍未通過此門檻，API 與介面目前停用桌面執行；不會自動切換成共用滑鼠／鍵盤。

## 啟動

在 macOS 上安裝 Python 環境管理工具 `uv` 與 Node.js 22+，然後：

```bash
./start.sh
```

開啟 [本機工作台](http://127.0.0.1:8765)。首次啟動會安裝 Python／前端依賴及 Chromium。服務只綁定 `127.0.0.1`。

已安裝完成時，可快速啟動後端：

```bash
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8765
```

開發介面使用兩個終端機：

```bash
# 後端
.venv/bin/python -m uvicorn server.app:app --host 127.0.0.1 --port 8765

# 前端
cd frontend
npm run dev
```

## 先使用指定的三個本機模型

在「模型與感知」選擇 Ollama，設定你已安裝的模型 ID：

| 模型 | 專案中的用途 | 驗證方式 |
| --- | --- | --- |
| `qwen3-vl:2b` | 任務規劃、選擇動作 | 先關閉影像輸入，只看文字與元素 ID |
| `minicpm-v4.6:latest` | 小模型規劃基準 | 相同任務、相同成功判定，分開計分 |
| `glm-ocr:latest` | 獨立的畫面文字辨識 | 讀取截圖，輸出文字；定位框由本機 OCR 提供 |

Ollama API Base URL 可填 `http://127.0.0.1:11434` 或相容的 `/v1` 路徑。Ollama 接頭實際使用原生 `/api/chat`，以設定 JSON 輸出、上下文與生成上限。[Ollama API](https://docs.ollama.com/api/chat)

MiniCPM 與 Qwen3-VL 原本都是多模態模型。關閉影像輸入能驗證「規劃只依賴文字」的管線，但不能因此推論所有純文字模型都會有相同表現。模型仍需能理解任務並輸出正確 JSON。[MiniCPM](https://ollama.com/library/minicpm-v4.6)、[Qwen3-VL](https://ollama.com/library/qwen3-vl:2b)

GLM-OCR 使用本機端點，且拒絕雲端模型轉送。在目前安裝的版本中，真實合成畫面測試能讀到所有指定文字，但回應可能重複 Markdown 結尾直到生成上限。程式會保留有效文字、標示 `transcript_complete=false`，並使用本機 OCR 的真實定位框，不把未定位的文字變成捏造座標。詳見 [感知設定與實測](docs/perception.md)。

## 模型相容性

支援 OpenAI Chat Completions、Anthropic Messages、Gemini generateContent、Ollama 與自訂 OpenAI 相容服務。模型名稱與 Base URL 可自行設定，不硬編碼模型白名單；自訂協定可新增 provider adapter。

API 金鑰由設定視窗輸入，只留在服務記憶體中，不寫入瀏覽器儲存空間或任務匯出。連線測試會發送一個短模型請求；雲端服務可能依自己的價格計費。若 `vision=false`，模型接頭不傳送截圖；畫面辨識的文字與任務仍會傳至所選端點。

[OpenAI 影像輸入](https://developers.openai.com/api/docs/guides/images-vision)、[Anthropic Messages](https://platform.claude.com/docs/en/api/messages/create)、[Gemini generateContent](https://ai.google.dev/api/generate-content)

## 操作與任務控制

- 起始網址：工作台的新瀏覽器任務固定從 `https://www.google.com/` 開始，之後可依任務前往其他網站；本機示範仍使用內建測試頁。
- 瀏覽器：網頁導覽、輸入、點擊／右鍵／雙擊、拖曳、捲動、快捷鍵、原生選單、多分頁與歷史導覽；代理專屬游標會沿平滑軌跡逐點送出真實事件。
- 檔案：上傳本次明確允許的本機檔案（每檔最多 64 MiB），支援原生欄位及可見按鈕開啟的隱藏檔案選擇器；HTTP／Blob 下載另存至各任務的輸出目錄，不自動開啟檔案。
- 桌面：可明確列舉並選擇 PID／視窗 ID，顯示獨立輸入與代理游標的能力診斷。Cua 背景候選驅動尚未通過驗證，執行維持停用。
- 感知：瀏覽器 DOM、Apple Vision OCR／RapidOCR、GLM-OCR 文字、可選的 YOLO-World 本機物件框。
- 控制：逐步確認或自動執行、暫停／繼續、補充指令、等待使用者回覆、停止、最多 200 步。
- 人工接手：可開啟獨立的瀏覽器視窗，遇到登入或驗證時自行操作，再於任務中回覆繼續。
- 修正：每個動作後重新觀察；錯誤回饋給模型；動作前重新核對目標；重複失敗會停止；完成前再次觀察並要求模型確認。
- 狀態回饋：回傳操作後的實際欄位值、勾選狀態及新文字，區分「輸入已送出」與「預期結果已發生」。
- 紀錄：代理瀏覽器的即時真實畫面、元素列表、操作事件、JSON 匯出。最近 40 個任務留在記憶體中，重啟後清除。

原生驅動的原始碼核對發現，部分「背景」點擊仍會短暫切換焦點，部分 AX 操作只在搶焦點後還原。這些行為不符合本專案的輸入獨立要求。需要前景操作的應用程式，必須改走代理專用的隔離桌面／遠端工作階段；目前尚未有可驗收的 Windows 環境。

瀏覽器平滑輸入與即時畫面的實測見 [輸入契約](docs/browser-input.md)。即時監看直接擷取 Chromium 畫面，模型規劃使用的 observation 另行保存，不會因監看而偷偷改寫。

[剪貼簿來源稽核](docs/browser-clipboard-audit.md)確認目前固定版本的一般 Mac `Meta+C/X/V` 使用瀏覽器程序內剪貼簿；特殊 Find pasteboard、任意按鍵及動態共存尚未全面驗證。此核查未讀取或修改使用者剪貼簿。

## 驗證

```bash
.venv/bin/python -m pytest tests benchmarks/test_benchmarks.py -q
npm --prefix frontend run build
npm --prefix frontend test
```

真實模型 benchmark 會呼叫你指定的 Ollama，並操作隔離 Chromium。成功由表單／資料儲存狀態獨立判定，不採信模型的「完成」文字。測試包括多欄位報名、篩選後修改正確庫存列、跨分頁查找與填回資料。

```bash
.venv/bin/python scripts/benchmark_models.py --model 'qwen3-vl:2b' --ocr-engine native
.venv/bin/python scripts/benchmark_models.py --model 'minicpm-v4.6:latest' --ocr-engine native

# OCR-only：不把 DOM 文字／元素交給規劃模型
.venv/bin/python scripts/benchmark_models.py --model 'qwen3-vl:2b' --no-dom --ocr-engine native

# GLM-OCR 與規劃模型分工
.venv/bin/python scripts/benchmark_models.py --model 'qwen3-vl:2b' --ocr-engine glm_ocr
```

執行時間、動作、錯誤、最終資料與各項判定都寫入 JSON。這些測試是可重現的局部能力證據，不代表已證明任何網站、任何應用程式或任何任務皆可成功。[測試說明](benchmarks/README.md)

原生桌面保留舊版 AppKit／全域輸入的實驗測試，真實儲存事件及檔案內容由 oracle 檢查。這些結果不能用來認證獨立輸入；目前停止在使用者的共用桌面執行此類測試。參見 [原生測試](benchmarks/native.md)。成熟度範圍與缺口見 [成熟門檻](docs/maturity.md)。

Hermes 已建立獨立設定與子程序 IPC 候選接頭。[純協定測試](docs/hermes-integration.md)沒有呼叫 AI 或操作電腦；另有 [Hermes→核心 Gateway→真實無頭瀏覽器整合](docs/core-gateway.md)，reviewed run2 使用腳本模型完成 6 次 API 往返、2 次實際動作。父程序獨立讀回繁中文字與一次儲存，DB 重開保留 SUCCEEDED，Hermes 結束已清理瀏覽器。AI 呼叫為 0；原首輪與後續 harness 失敗均保留，這些候選整合不是一般任務或真實小模型能力驗收。

[本機模型核心整合](docs/hermes-model-runtime.md)新增 Ollama JSON 工具接頭、每任務認證的本機模型端點、四個 Hermes 工具，以及父程序註冊的瀏覽器 Verifier。它使用同一個 TaskStore／Gateway；驗證失敗可重新觀察並修正，模型文字不能寫入 SUCCEEDED。新整合的正式工作台切換尚未完成。[最新兩個 profile 任務](artifacts/hermes-model-profile-v2-run2/summary.json)為 **0/2**：Qwen 已做 5 次操作，但座位選擇錯誤且逾時；MiniCPM 沒有執行操作。兩者 planner 影像為零、實際 context=64000；與原 legacy 六案基線分開記錄，不能視為小模型成熟度通過。此固定 Hermes 版本目前要求至少 64000 context，較小上下文模型尚未接通。

新核心的完整任務測試保留原案例與 oracle，報告另存且禁止覆寫：

```bash
.venv/bin/python scripts/benchmark_hermes.py --model 'qwen3-vl:2b' --case profile --ocr-engine glm_ocr --output artifacts/hermes-profile-new-run
```

[共用契約與 SQLite TaskStore 候選](docs/core-storage.md)已連接 Policy／ActionGateway 與 driver 輸入前 guard。SQLite v4 支援 v2／v3 保留資料遷移，保存政策 digest，並在同庫提供選配的[無損模型歷史](docs/model-history-views.md)；未知結果禁止盲重播。候選 Gateway 已能暫停並保留背景瀏覽器，需重新觀察才能恢復輸入；[Hermes 對話的暫停／恢復](docs/hermes-pause-design.md)仍待接入。最新[完整程式回歸](artifacts/candidate-core-v3-regression.json)為 **1021 passed、1 skipped**，模型回覆在測試中使用替身。歷史版本與真模型結果另見[版本化證據](docs/hermes-model-runtime.md)，不與測試數量混計。依 [ADR 0001](docs/architecture.md)，下一步接入唯一正式執行出口；工作台仍使用記憶體 Run，正式 Verifier 設定／核準介面與重啟後自動恢復尚未完成。

## 結構

```text
frontend/             工作台與模型設定
server/app.py         本機 API、工作階段與請求邊界
server/agent.py       觀察 → 決策 → 核對 → 操作 → 驗證循環
server/providers.py   模型協定接頭
server/hermes_bridge.py  Hermes 隔離子程序候選接頭
server/core/          契約、SQLite／Policy／ActionGateway 候選（未接正式 Run）
server/background.py  獨立輸入啟用門檻
server/cua_driver.py  明確視窗的 Cua 背景候選驅動
server/perception.py  OCR／GLM-OCR／YOLO-World
server/drivers.py     瀏覽器與舊版全域輸入實驗驅動
server/accessibility.py  macOS AX 語意資訊
server/grounding.py   新舊畫面的操作目標核對
benchmarks/           任務頁面與獨立成功判定
tests/                執行控制、API、感知與驅動測試
```

規劃以 Windows 11 為主要驗收平台；目前開發機是 macOS，Windows／Linux 的實機驗收尚未開始。多顯示器、自繪介面、複雜拖曳、登入及長時間跨應用程式任務仍需擴充實測覆蓋。YOLO-World 接頭需要準備本機權重與適合介面的詞彙，目前沒有下載或認證其 UI 辨識能力。
