# Ollama 的 Qwen3-VL final channel 相容處理

本機 `qwen3-vl:2b` 在 Ollama 0.32.9 的 metadata 使用 `qwen3vl` family、`qwen3-vl-thinking` renderer 與 parser。此組合在原生 `/api/chat` 配合 `think:false` 和 `format:"json"` 時，可能完成生成但 `message.content` 為空。移除 JSON format、改用 OpenAI compatibility 或設定 `reasoning_effort:"none"`，本次合成測試仍未產生 final content。

`server/providers.py` 先用 `/api/show` 確認上述三項 metadata，然後在 chat messages 尾端加入空 assistant prefill：

```text
<think>

</think>

```

這段只有關閉的空區塊，沒有任何操作、目標、答案或模型推理文字。生成結果仍只從 `message.content` 讀取，經 `Action` schema 驗證後才可執行；`message.thinking` 永遠不作為操作來源。不更換模型、不建立別名、不修改或下載權重，影像仍受 `vision` 開關控制。

Ollama 的 [Qwen3-VL renderer 原始碼](https://github.com/ollama/ollama/blob/v0.32.9/model/renderers/qwen3vl.go) 將最後一則 assistant message 視為 prefill。官方 [thinking API 說明](https://docs.ollama.com/capabilities/thinking) 定義 final content 與 thinking 分開。這是針對已辨識 renderer 的相容處理；其他 family、非 thinking renderer 或沒有 `/api/show` 的相容服務仍使用原本的 chat request。

Metadata 查詢最多 5 秒，查詢與生成共用 120 秒總期限，停止任務可取消兩者。查詢失敗會回到標準 chat；模型仍無有效 final JSON 時回報錯誤，不從私有推理補答案。

Ollama 現在以 `Action` 的 JSON schema 限制回覆結構，但傳輸時移除 `maxLength`、`title`、`default`。大型字串長度限制可能讓 llama.cpp 的 grammar 編譯失敗（[上游議題](https://github.com/ggml-org/llama.cpp/issues/25923)）；本機原始 schema 也回 HTTP 400，移除後合成測試回 HTTP 200，1.146 秒產生合法輸入操作，證據為 `artifacts/qwen-schema-compatibility.json`。所有長度、座標、URL 和動作必填限制仍由完整的 Pydantic `Action` 在執行前檢查。若舊服務回報 grammar/format HTTP 400，只在同一總期限内回退 JSON mode 一次。

欄位驗證錯誤只回報已知欄位名與錯誤碼，協助模型修正；不回顯錯誤值、未知欄位名稱或模型原文。

每次 `next_action` 會以 `server/action_space.py` 建立嚴格的 `oneOf` 操作規格：各操作的必要參數不能為空，目標 ID 只來自本次模型觀察的最多 250 個元素，HTML 選單的選項則限制為該選單實際列出的 label/value。短篇公開操作目的 `reason` 在操作種類前生成；不要求私有推理。文字框及選單仍可合法點擊以聚焦或開啟，座標操作保留低階輸入能力。此 helper 不讀取使用者任務或 benchmark 答案，也不形成另一個決策迴圈，可由單一 ActionGateway 的工具介面重用。

`server/key_contract.py` 在模型回覆階段驗證真實按鍵名稱與 hotkey 結構，依 browser、平台及 driver 提供的 `key_capabilities` 縮小範圍。像 `key:"select_option"`、`A+B`、macOS 桌面的 `F24` 均會在 driver 前被拒絕；Cua 候選後端的空能力清單不會繼承全域鍵盤能力。實際 headless 鍵盤測試另發現 Playwright 不接受 `F13`～`F24`，瀏覽器規格已縮至 `F1`～`F12`，測試會逐一執行所有宣告的單鍵與代表性 hotkey、檢查頁面鍵盤事件。這是參數契約與隔離瀏覽器輸入證據，不表示各作業系統的實體輸入已經驗收。

模型視圖只會移除文字完全相同、且 OCR 框大部分與 DOM 框重疊的重複項；原始觀察及 driver targets 保留。即使服務忽略 schema 或退回 JSON mode，完整 Pydantic 與獨立 JSON Schema 驗證仍在執行前進行。

## 重現

以下診斷只使用合成文字表單，不操作桌面、不傳送真實畫面：

```sh
.venv/bin/python scripts/diagnose_ollama_protocol.py \
  --model qwen3-vl:2b \
  --variant native_json \
  --variant native_prefill_closed_think_json \
  --output artifacts/qwen-protocol-comparison.json
```

`artifacts/qwen-protocol-matrix.json` 保存原始 protocol matrix，`artifacts/qwen-protocol-raw.json` 保存 prefill 與 raw mode 對照。每份資料只記錄 final content、thinking 字數、請求選项與時間，不保存 thinking 內容。首次 prefill 合成測試在 1.474 秒內回傳合法點擊操作；實際多步任務能力需另看 benchmark oracle，不能由單次格式測試推論。

另測 native `think:true`、不加 format、不預填 assistant，將生成上限提高至 2048 與 4096。兩次皆至 token 上限而沒有 final content，耗時 39.19 / 84.46 秒。證據 `artifacts/qwen-thinking-2048.json`、`artifacts/qwen-thinking-4096.json`；不因這種回覆而執行 thinking 內容。

```sh
.venv/bin/python scripts/benchmark_models.py \
  --model qwen3-vl:2b --case profile --timeout 300 \
  --output artifacts/qwen-profile-prefill.json
```

此 benchmark 使用真正的 agent loop 與隔離瀏覽器，預設 `vision=false`；是否通過由 fixture 儲存的欄位及送出狀態判定，與模型的 `done` 宣告無關。單次合成協定通過不代表任務成功：`qwen-profile-prefill.json`、`qwen-profile-prefill-semantic.json`、`qwen-profile-prefill-feedback.json` 三次 profile 結果皆未通過；小模型仍有把點擊誤認為輸入、重複操作與舊 ID 的問題。這些失敗資料保留供後續評估。

`artifacts/local-model-matrix-grounded.json` 保留持續 DOM ID、動作效果回饋及 flat schema 的完整六案例矩陣：兩個決策模型各跑 profile、inventory、supplier，**0/6 通過**。`artifacts/local-profile-affordance.json` 是後續嚴格結構的兩個 profile，**0/2 通過**；當時過度排除文字框 focus click，Qwen 改點錯誤按鈕，MiniCPM 產生不合法的 `key:"select_option"`。目前已修正這兩個規格問題，但沒有據此宣稱多步任務能力改善。上述 `Run` 測試屬於 prototype planner baseline，不能替代計畫要求的 Hermes 主流程驗收。
