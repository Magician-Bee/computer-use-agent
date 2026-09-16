# Browser clipboard source audit

2026-09-11；macOS arm64；只讀原始碼與執行檔中繼資料。沒有啟動瀏覽器、執行複製／剪下／貼上、讀取或改寫使用者剪貼簿。本次唯一新增檔案是這份文件，runtime 未修改。

目前精確版本的 headless shell 原始碼顯示：普通 `Meta+C`、`Meta+X`、`Meta+V` 的瀏覽器編輯路徑使用 `ClipboardNonBacked`，其資料保存在瀏覽器程序的記憶體中。這是有版本依據的靜態結論，不是由 `headless=True` 一詞推論，也不是已完成動態共存驗收。macOS 另有直接使用 OS Find pasteboard 的特殊路徑；本次未證明目前 Action 能否觸發，因此不能宣稱所有剪貼簿 API／任意快捷鍵皆已隔離。

本次可辨識的安裝內容：

| 項目 | 讀取結果 |
| --- | --- |
| Playwright Python | `1.62.0` |
| Playwright browser revision | `1234` |
| `browsers.json` 指定版本 | `151.0.7922.34`；`chromium-headless-shell` |
| 執行檔內版本字串 | 包含 `151.0.7922.34` |
| 平台 | Mach-O arm64 |
| 執行檔大小 | `163329264` bytes |
| 執行檔 SHA-256 | `7687bff7cb2db075f250e6d5848bbc8838cac3802ac3952a899c574f8eccab45` |
| Code signature | `adhoc, linker-signed`；`TeamIdentifier` 未設定 |
| 對應 Chromium tag | `151.0.7922.34` |
| tag 解析到的 commit | `782af9cb30a53f54487e5d2e44738645a8ec457c` |

執行檔位於 [chrome-headless-shell](/Users/example/Library/Caches/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-mac-arm64/chrome-headless-shell)。版本設定來自 [已安裝 browsers.json](/Users/example/Desktop/ComputerUSE-Agent/.venv/lib/python3.11/site-packages/playwright/driver/package/browsers.json)。官方 [精確版本 tag](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34) 的 Gitiles JSON 提供上述 commit；對應原始碼檔以 Gitiles `?format=TEXT` 讀取並解碼。

這份雜湊固定的是本次讀到的本機檔案。沒有重新下載官方 artifact 比對、重現 Chromium build，或驗證此二進位檔與 tag 的密碼學對應；adhoc 簽章也不提供發行者身分證明。版本字串與 Playwright manifest 相符，可作為來源核查依據，不能當成二進位實作的完整認證。runtime 目前亦未在每次啟動前核對這個 SHA-256。

`BrowserDriver.start()` 每個實例各自呼叫 `chromium.launch(headless=self.headless)`，未指定 `channel` 或自訂 executable，再建立自己的 context。已安裝 [Playwright coreBundle.js](/Users/example/Desktop/ComputerUSE-Agent/.venv/lib/python3.11/site-packages/playwright/driver/package/lib/coreBundle.js:43099) 的 `getExecutableName` 在沒有 channel 且 headless 時選擇 `chromium-headless-shell`；該檔第 32403–32410 行將 mac-arm64 對應到上述 executable 路徑。這與 [Playwright 官方 headless shell 說明](https://playwright.dev/python/docs/browsers#chromium-headless-shell) 一致。讀取時沒有 `PLAYWRIGHT_BROWSERS_PATH` override。此結論限於這條啟動路徑，不能套用到 headed、指定其他 channel、連接外部現成瀏覽器或 legacy desktop driver。

精確 tag 的原始碼鏈如下：

1. [HeadlessContentBrowserClient::CreateBrowserMainParts，第 187–189 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/headless/lib/browser/headless_content_browser_client.cc#187) 建立 `HeadlessBrowserMainParts`。其 [PreMainMessageLoopRun，第 27–34 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/headless/lib/browser/headless_browser_main_parts.cc#27) 在執行 browser main loop 前，無條件呼叫 `SetHeadlessClipboardForCurrentThread()`。
2. [headless_clipboard.cc，第 20–45 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/components/headless/clipboard/headless_clipboard.cc#20) 定義 `HeadlessClipboard : ClipboardNonBacked`，並透過 `ui::Clipboard::SetClipboardForCurrentThread` 安裝。其 [基底類別說明，第 28–40 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/ui/base/clipboard/clipboard_non_backed.h#28) 明確將它定義為不與平台同步、實例消失後資料不保留的記憶體內實作。
3. [Clipboard::SetClipboardForCurrentThread / GetForCurrentThread，第 140–163 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/ui/base/clipboard/clipboard.cc#140) 使用程序內的 thread-ID map；已有實例時讀取它。這不是以 BrowserContext 為 key 的隔離。每個 `BrowserDriver` 分別啟動 browser process，才構成目前 driver 之間的程序分隔；不能把同一 browser process 下的不同 context 說成各有獨立剪貼簿。
4. 普通 [Blink Copy / Cut / Paste 命令](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/third_party/blink/renderer/core/editing/commands/clipboard_commands.cc#361) 經 `SystemClipboard` 執行讀寫；[SystemClipboard](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/third_party/blink/renderer/core/clipboard/system_clipboard.cc#108) 透過 ClipboardHost IPC，browser 端 [ClipboardHostImpl](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/content/browser/renderer_host/clipboard_host_impl.cc#95) 讀取 `ui::Clipboard::GetForCurrentThread()`，寫入則交由 [ScopedClipboardWriter，第 70–73 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/ui/base/clipboard/scoped_clipboard_writer.cc#70) 使用相同實例。
5. 此 tag 自帶 [HeadlessBrowserTest.ClipboardCopyPasteText，第 277–312 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/headless/test/headless_browser_browsertest.cc#277)，檢查目前 clipboard 可作為 `ClipboardNonBacked` 使用，再測各 buffer 的讀寫。本次只讀了測試原始碼，沒有執行該上游測試。

Mac 按鍵必須區分 `Meta` 和 `Control`。已安裝 [macEditingCommands / Chromium RawKeyboardImpl](/Users/example/Desktop/ComputerUSE-Agent/.venv/lib/python3.11/site-packages/playwright/driver/package/lib/coreBundle.js:35540) 把 `Meta+KeyC`、`Meta+KeyX`、`Meta+KeyV` 分別映射為 copy、cut、paste，並作為 `Input.dispatchKeyEvent` 的編輯 commands 傳給 Chromium，而不是送 OS 實體按鍵。相同 map 的 `Control+KeyV` 是 `pageDown:`；`Control+KeyC`／`Control+KeyX` 沒有對應 copy／cut 映射。它們仍可能交給頁面或平台預設按鍵處理，不能將 Mac 上的 Ctrl 與 Cmd 視為同義。Windows／Linux 的 Ctrl+C/X/V 路徑未在本次做平台驗收。

Find pasteboard 是已找到的實作限制，應與普通複製貼上分開看待：[WebLocalFrameImpl::CopyToFindPboard，第 881–885 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/third_party/blink/renderer/core/frame/web_local_frame_impl.cc#881) 呼叫 [SystemClipboard::CopyToFindPboard，第 426–431 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/third_party/blink/renderer/core/clipboard/system_clipboard.cc#426)，最終 [ClipboardHostImpl::WriteStringToFindPboard，第 26–31 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/content/browser/renderer_host/clipboard_host_impl_mac.mm#26) 直接交給 Cocoa `FindPasteboard`。該 [FindPasteboard 實作，第 25–76 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/ui/base/cocoa/find_pasteboard.mm#25) 初始化時讀取 `NSPasteboardNameFind`，更新時清除並寫入它；沒有經過 `ClipboardNonBacked`。這條 OS-backed API 路徑確實存在，但本次沒有證明當前 Action／headless shell 可觸發它：目前 Playwright Mac editing map 沒有 `Meta+E` 或 `CopyToFindPboard` 映射，普通 C/X/V 也不呼叫這條路徑。沒有實際觸發或測試 Find pasteboard。

同一個 [Mac ClipboardHost 的 GetPlatformPermissionState，第 35–71 行](https://chromium.googlesource.com/chromium/src/+/refs/tags/151.0.7922.34/content/browser/renderer_host/clipboard_host_impl_mac.mm#35) 在對應 renderer flag 啟用時，會讀取 `NSPasteboard.generalPasteboard.accessBehavior`。這是 OS 剪貼簿存取權限中繼資料，不是剪貼簿內容；本次未檢查該旗標於執行中的值，也未呼叫此 API。

可交付的判定為：**普通 Meta+C/X/V 在上述精確 headless shell 原始碼中的主要資料路徑是程序內的非平台剪貼簿；每個 driver 的獨立 browser process 提供相應隔離依據。所有特殊 API、任意按鍵、二進位與來源一致性、不同平台，以及使用者與 agent 的動態剪貼簿共存，仍未全面驗證。** 本文件不是 release Gate，也不把 headless 或 context isolation 當成完整 OS 剪貼簿隔離保證。
