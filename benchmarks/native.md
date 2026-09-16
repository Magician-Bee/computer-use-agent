# 原生 macOS Computer Use 測試

這個 benchmark 啟動獨立的 AppKit 程式，包含真正的 `NSTextField`、`NSButton` checkbox 與 Save 按鈕。決策走正式 `Run`，觀察與動作走 `DesktopDriver` 子類，使用本機 OCR／AX；`vision=false`，決策模型不會收到圖片。模型模式沒有固定答案動作序列；獨立的 `--driver-smoke` 模式則是明確標示的驅動自測，不呼叫模型。

執行前需要 macOS「輔助使用」及「螢幕錄製」權限、基本專案依賴，以及已安裝且可運行的本機 Ollama 模型。程式不下載模型。從專案根目錄執行：

```bash
.venv/bin/python scripts/benchmark_desktop.py --run --production-input --model qwen3-vl:2b
```

另一個模型需分開執行，避免同時控制畫面：

```bash
.venv/bin/python scripts/benchmark_desktop.py --run --production-input --model minicpm-v4.6:latest --timeout 600
```

需要同時測試 GLM-OCR 輔助文字：

```bash
.venv/bin/python scripts/benchmark_desktop.py --run --production-input --model qwen3-vl:2b --ocr-engine glm_ocr --timeout 600
```

`--help` 只顯示說明；沒有 `--run` 時，程式會在啟動視窗／連線模型前退出。預設最多 16 個操作、300 秒，模型要求使用者介入時停止。執行期間請讓專用視窗留在前景；切換到其他程式會讓測試中止。

## 如何判定成功

任務要求將 Project name 改成 `ComputerUSE Native Verified`、開啟 Enable notifications，再滑鼠點擊 Save project。預期值是正常使用者任務內容，沒有透過模型看不到的接口改表單。控制項的 AX label 對應畫面可見文字。

只有 fixture 的原生 Save 按鈕收到左鍵 mouse-up 事件，才會把**當前控制項值**寫入 `saved-project.json`。模型沒有檔案或 shell 工具，也不會收到 oracle 的 JSON、nonce 或檔案路徑。測試獨立讀取該檔，檢查正確名稱、checkbox 狀態、啟動 nonce、來源 PID 與原生 Save click。`passed` 同時要求 oracle 通過且模型回報完成；`oracle_pass` 則獨立列出是否真的完成表單操作。

每次結果放在新的 `artifacts/native-模型名-時間-亂數/` 資料夾：

- `report.json`：獨立 oracle 檢查、模型狀態、步數、完整 session 與輸入界線拒絕紀錄。
- `saved-project.json`：只有真正點擊原生 Save 才存在。
- `fixture-ready.json`：僅含專用 PID、window ID、視窗範圍與啟動識別；沒有預期表單答案。
- `frames/*.png`：實際觀察圖片；專用視窗內容外全部遮蔽。
- `fixture.log`：fixture 啟動錯誤。

exit code `0` 表示 oracle 與模型完成狀態都通過，`1` 表示已產生報告但測試未通過，`2` 表示環境／啟動／界線等錯誤。

## 驅動自測與操作範圍

先驗證正式輸入驅動，可執行：

```bash
.venv/bin/python scripts/benchmark_desktop.py --run --driver-smoke --production-input --ocr-engine native --project-name 'ComputerUSE 中文輸入驗證' --timeout 60
```

`--driver-smoke` 透過同一個 Run 觀察／執行迴圈，從當前 AX／OCR 元素依序找欄位、全選、輸入、勾選及儲存。報告固定標示 `planner_mode=scripted_driver_smoke`、`model=null`、`model_calls=0`，不列為模型成功；oracle 仍要求實際原生 Save mouse-up 與正確表單內容。它不讀取 oracle 來決定動作。

`--production-input` 使用正式 DesktopDriver 的 PyAutoGUI 滑鼠、公開 Quartz 鍵盤 API 與原生剪貼簿備份／還原。在每次輸入前，額外檢查測試子行程仍存活、最新前景 PID 正確、window ID 與位置未變，以及視窗沒有被其他程式遮住；座標也必須位於專用內容範圍。這是有界的實際全域輸入，檢查與輸入間仍可能發生競爭；請讓專用視窗保持前景。此模式通過時 `production_desktop_input_validated=true`，只表示這個原生表單的正式輸入路徑已通過。

未指定 `--production-input` 的診斷模式使用 `CGEventPostToPid`，另透過 fixture 專用的私有 `CGEventSetWindowLocation` 做視窗座標路由。它不驗證正式全域輸入，也可能不相容其他 macOS 版本；報告的 `input_transport` 明確區分。正式 DesktopDriver 不使用此私有符號。

兩種模式都只接受左鍵 click／double-click、move、單行 type、表單編輯按鍵與 wait；阻擋開程式、導覽、系統捷徑、分頁、drag，以及其他視窗座標。截圖按 window ID 只擷取專用視窗，再遮蔽其內容外區域；AX 元素也依相同範圍過濾。Dock 的透明系統覆蓋層需額外通過暫存像素可見性比對；比對影像不儲存、不送给模型。結束只終止自己啟動的子行程。

正式輸入自測的實際通過紀錄是 `artifacts/native-driver-smoke-production-quartz/report.json`：5 個動作、15.855 秒，正確中文名稱、checkbox=true、原生 Save mouse-up、來源 PID 與 nonce 均通過，沒有模型呼叫。這不能證明模型規劃能力或其他應用程式的可靠性；真實模型結果另以 `planner_mode=real_model` 報告為準。

建立 fixture 使用 [Apple NSApplication／AppKit](https://developer.apple.com/documentation/appkit/nsapplication) 與 [PyObjC 官方原生視窗範例](https://pyobjc.readthedocs.io/en/latest/examples/core/Scripts/HelloWorld/) 的原生控制項流程。正式快捷鍵透過 [公開 CGEventFlags](https://developer.apple.com/documentation/coregraphics/cgeventflags) 明確指定 Command／Shift／Control／Option，並在中止時釋放已按下按鍵。每份新報告也記錄程式來源雜湊、相依版本與 `false_completion`，區分口頭完成與 oracle 失敗。
