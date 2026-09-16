# 讓純文字 LLM 看懂桌面

ComputerUSE 把「觀察畫面」與「決定下一步」分開：本機 OCR 取得文字與位置，選配 YOLO-World 補上物件位置，再將目標 ID、中心點、尺寸、信心分數交給 LLM。LLM 回傳動作後，由桌面或瀏覽器 driver 解析 ID 並執行；下一步重新觀察。純文字 LLM 不需要處理圖片，也不需要估算螢幕座標。

## 模式

| 設定 | 瀏覽器 | 桌面 |
| --- | --- | --- |
| `auto` | DOM 語意元素 | 本機 OCR，加上 driver 可取得的輔助使用元素 |
| `ocr` | DOM + 本機 OCR | 本機 OCR + 可用的輔助使用元素 |
| `ocr_yolo` | DOM + OCR + 本機 YOLO-World | OCR + 本機 YOLO-World + 可用的輔助使用元素 |

`vision=false` 時，provider 只收到文字觀察與元素資料；截圖仍在本機介面預覽。OCR 和 YOLO 均在本機推論。不過，辨識出的畫面文字仍會傳給你選擇的 LLM；如需整條流程在本機執行，請搭配本機 Ollama 或相容端點。

## 使用已安裝的 GLM-OCR

`ocr_engine` 可以選擇 `auto`／`native`（快速原生 OCR）或 `glm_ocr`。選擇後者時，預設以 `glm-ocr:latest` 經由 `http://127.0.0.1:11434/api/generate` 辨識畫面文字；`ocr_model`、`ocr_base_url` 可以在模型設定中調整。此端點必須是 loopback 本機位址，服務不跟隨轉址、不使用環境代理，並在傳送圖片前查詢 `/api/show`，拒絕將雲端模型別名當作本機 OCR。

GLM-OCR 是專門的畫面文字辨識器；`minicpm-v4.6:latest` 或 `qwen3-vl:2b` 可以另外擔任決策模型。即使決策模型設定為純文字模式，GLM-OCR 仍能在本機處理圖片，再提供文字結果。實作使用官方指定的 `Text Recognition:` 提示詞與 Ollama 原生 `/api/generate` 介面，每次本機請求有 120 秒總上限，不自動下載模型。[GLM-OCR 官方模型卡](https://huggingface.co/zai-org/GLM-OCR)、[官方 Ollama 部署方式](https://github.com/zai-org/GLM-OCR/blob/main/examples/ollama-deploy/README.md)

GLM-OCR 的 transcript **不附可直接點擊的座標**。本程式會同時執行原生 OCR，保留原始截圖上的文字框、DOM 與 AX 元素，將 GLM transcript 額外標記為未受信任的畫面內容。所有 `ocr_N` 框仍來自 `apple_vision`／`rapidocr`，不會根據模型敘述猜測座標或創造 `glm_N` 目標。`perception.grounding_engine` 記錄定位引擎；`transcript_grounded=false` 表明 GLM 文字不能單獨作為座標依據。

本機測試發現，已安裝的 GLM-OCR 在辨識完成後可能反覆輸出 Markdown 結尾並碰到 token 上限。程式會保留已辨識文字，只移除連續重複的結尾分隔符，明確回報 `transcript_complete=false`、終止原因及 `warning`，繼續使用原生 OCR 的已定位目標；不會把這種輸出宣稱為完整辨識。原始診斷保存在 `artifacts/perception-glm-diagnostic.json`。

一般測試不會呼叫 Ollama。明確啟用本機模型測試：

```bash
COMPUTERUSE_TEST_OLLAMA=1 python -m pytest tests/test_perception.py -k live_glm -q
```

`artifacts/perception-glm-synthetic.png` 是程式產生的設定頁測試圖，不是桌面截圖；對應的 `artifacts/perception-glm-smoke.json` 記錄實際本機辨識結果與原生目標位置。

### 純文字模型的單次操作驗證

`scripts/diagnose_perception_action.py` 在 headless 瀏覽器建立兩個按鈕、互換左右排列，刪除 DOM 元素與 executable handles 後執行真正 OCR。決策模型設定 `vision=false`，只收到 OCR 文字、框與未定位的 GLM transcript；再以 production `next_action` 提議一次操作。測試只允許一次左鍵點擊，是否點中由頁面 click listener 的獨立記錄判定，不看模型宣告。隱藏 oracle 內容不會傳給模型。

```bash
.venv/bin/python scripts/diagnose_perception_action.py \
  --model qwen3-vl:2b --model minicpm-v4.6:latest \
  --ocr-engine native --ocr-engine glm_ocr --seed 17 --seed 18 \
  --output artifacts/perception-action-conformance.json
```

2026-09-11 的本機證據：

| 決策模型（均為純文字模式） | Apple Vision OCR | GLM-OCR + Apple Vision 定位 |
| --- | --- | --- |
| `qwen3-vl:2b` | 2/2，左右位置皆點中 | 2/2，左右位置皆點中 |
| `minicpm-v4.6:latest` | 0/2，產生 scroll 而不是 click | 0/2，產生 scroll 而不是 click |

共 **4/8 單次工具操作通過**。每次決策請求均確認圖片數為 0、DOM 元素數為 0、不含 oracle 程式名；模型回覆由 HTTP 200 的 schema mode 取得。MiniCPM 的錯誤操作沒有執行，頁面點擊記錄保持空白。GLM 四次 transcript 仍標記不完整，所有點擊座標都由 Apple Vision 提供。JSON 記錄原始碼 import-time SHA256、實際 final action、每次 oracle、耗時及合成截圖；不保存私有 thinking。這只能證明此工具回合的協定、定位與輸入，不代表整個任務成功，也不證明 GLM 優於原生 OCR 或 YOLO 已完成實測。

## 安裝 OCR

macOS 的基本安裝已包含 `pyobjc-framework-Vision`、`pyobjc-framework-Quartz` 和 Pillow。此實作透過 PyObjC 呼叫 Apple Vision 的 `VNRecognizeTextRequest`，優先使用精確辨識及系統支援的繁中、簡中、英文，不會另下載 OCR 權重。[Apple 文字辨識文件](https://developer.apple.com/documentation/vision/recognizing-text-in-images)、[PyObjC Vision 支援](https://pyobjc.readthedocs.io/en/latest/apinotes/Vision.html)

Windows／Linux 或需要備援引擎時，在專案虛擬環境執行：

```bash
python -m pip install 'rapidocr-onnxruntime>=1.3,<2'
```

這裡刻意使用附有 ONNX 權重、回傳 tuple API 的 1.x 發行套件；圖片以記憶體陣列送入，不使用網址輸入。macOS 優先採用 Vision，若 Vision 執行失敗且 RapidOCR 已安裝，則使用 RapidOCR。沒有 OCR 引擎時會回報安裝方式，不會假裝完成辨識。[RapidOCR 1.x API](https://rapidai.github.io/RapidOCRDocs/v1.4.4/install_usage/api/RapidOCR/)

驗證方式：

```bash
python -m pytest tests/test_perception.py -q
```

測試涵蓋座標轉換、邊界裁切、保留 DOM／AX 元素、模型缺失及真實 OCR。OCR 測試以 Pillow 生成 `OPEN SETTINGS` 圖片，執行本機 OCR 並驗證文字與目標位置，不會擷取或操作使用者桌面。若沒有 OCR 引擎，只有這項真實推論測試會跳過。

## 啟用 YOLO-World

YOLO 是選配，OCR 不需要先安裝 PyTorch 或 YOLO。需要偵測物件時：

```bash
python -m pip install -r requirements-perception.txt
export COMPUTERUSE_YOLO_MODEL='/absolute/path/to/yolov8s-worldv2.pt'
```

從 [Ultralytics YOLO-World 模型頁](https://docs.ultralytics.com/models/yolo-world/) 選擇並自行下載相容的 `.pt` 權重，設定**已存在的本機絕對路徑**，重新啟動服務，再選擇 `ocr_yolo`。服務不接受權重網址，也不會自動下載缺失權重。`/api/health` 的能力資訊會提示 OCR 引擎及 YOLO 設定；`yolo=true` 代表依賴與檔案已具備，權重相容性會在首次推論時檢查。推論預設使用 CPU。

標準 YOLO-World 權重的離線類別主要是 COCO 物件，**不能因此宣稱它已受過桌面 UI 訓練**。它不會自動理解每個圖示代表的功能。文字按鈕通常依靠 OCR／AX／DOM 比較實用；自訂圖示可準備專用詞彙，再依實際介面驗證偵測品質。自訂詞彙不等於重新訓練模型。

### 一次性準備自訂詞彙

Ultralytics 支援先以 `set_classes()` 編碼詞彙，再儲存供離線推論使用的 checkpoint。這個步驟可能需要下載 CLIP 文字編碼器；因此服務執行時**不會**呼叫它，改由使用者明確執行獨立的準備工具。[離線詞彙與自訂類別](https://docs.ultralytics.com/models/yolo-world/)、[CLIP 載入實作](https://github.com/ultralytics/ultralytics/blob/main/ultralytics/nn/text_model.py)

```bash
python -m pip install 'git+https://github.com/ultralytics/CLIP.git'

python scripts/prepare_yolo_world.py \
  --model '/absolute/path/to/yolov8s-worldv2.pt' \
  --output '/absolute/path/to/ui-world.pt' \
  --classes 'button' 'text field' 'checkbox' 'gear icon' 'search icon' 'folder icon' \
  --allow-downloads

export COMPUTERUSE_YOLO_MODEL='/absolute/path/to/ui-world.pt'
```

`--allow-downloads` 明確允許這次準備程序下載 CLIP；沒有該參數就會在載入模型前停止。輸入 detector 權重仍必須已在本機。工具不覆寫既有輸出，會另存 `.labels.json` 方便確認類別；checkpoint 只保留已編碼的詞彙，移除不再需要的 CLIP 物件。完成後重新啟動服務，執行時只使用本機 checkpoint。

## 元素格式與座標

```json
{
  "id": "ocr_1",
  "role": "text",
  "text": "Open Settings",
  "x": 110.0,
  "y": 40.0,
  "width": 140.0,
  "height": 40.0,
  "confidence": 0.98,
  "source": "apple_vision"
}
```

`ocr_N` 表示辨識文字，`obj_N` 表示 YOLO 物件。`x/y` 是原始截圖中的中心點，原點在左上角；不是 CSS 點、macOS 邏輯點或 0–1 比例。Apple Vision 的左下角正規化框先轉成左上角像素框，接著裁切到截圖範圍；driver 負責將截圖像素換成實際輸入座標。若截圖宣告尺寸不一致，程式停止處理以避免錯點。[Apple Vision 座標定義](https://developer.apple.com/documentation/vision/vndetectedobjectobservation/boundingbox)

新增元素保留原本的 DOM／AX 元素，不修改傳入 observation；ID 僅對目前這張畫面有效。OCR 的框表示可見文字區域，不保證它是互動控制項；YOLO 也只是估計物件位置。每種來源最多輸出 250 個目標，信心分數低於 0.3 或無有效範圍的偵測不會成為操作目標。

若 executor 提供 `perception_exclusions=[{"kind":"executor_cursor","bbox":[x,y,width,height]}]`，其中座標為截圖左上角像素，完全位於游標覆蓋區的 OCR／YOLO 結果不會成為新目標。與游標部分重疊的結果以及原有 DOM／AX 元素保留，並標記 `visually_occluded_by`。覆蓋區內可能是游標，也可能有被遮住的真實文字／控制項，程式不能從該截圖區分，故會明確回報 `occluded_content:"unknown"`、`complete_visual_coverage:false` 及排除數量；不宣稱畫面辨識完整。GLM transcript 仍只是未定位的文字，不能填補這個座標缺口。

目前已在本機實測 Apple Vision 的合成圖片辨識。YOLO 的資料轉換及缺失模型行為有自動化測試；未隨專案下載大型 detector／CLIP 權重，也未宣稱已完成任何指定 UI 的 YOLO 準確率評測。Ultralytics 推論停用套件自動安裝與分析同步，設定 `YOLO_OFFLINE=true`；同步設定會由其 SDK 儲存。[Ultralytics 隱私設定](https://docs.ultralytics.com/help/privacy/)
