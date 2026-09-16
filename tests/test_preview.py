from types import SimpleNamespace

from server.preview import BrowserPreview


async def connected():
    return False


async def test_preview_reads_only_selected_page_without_touching_observation():
    calls = []
    observation = {"text": "model observation stays frozen"}
    class Page:
        def is_closed(self):
            return False
        async def screenshot(self, **kwargs):
            calls.append(kwargs)
            return b"live-frame"
    run = SimpleNamespace(request=SimpleNamespace(target="browser"), driver=SimpleNamespace(_page=Page()),
                          screenshot=lambda: (b"observation-frame", "image/png"), observation=observation, status="running")
    preview = BrowserPreview(run)
    assert await preview.frame() == (b"live-frame", "image/jpeg")
    assert await preview.frame() == (b"live-frame", "image/jpeg")
    assert len(calls) == 1
    assert run.observation is observation
    assert calls[0] == {"type": "jpeg", "quality": 70, "timeout": 1800}


async def test_terminal_preview_uses_saved_frame_and_ends():
    run = SimpleNamespace(request=SimpleNamespace(target="browser"), driver=None,
                          screenshot=lambda: (b"final-frame", "image/png"), status="completed")
    frames = [frame async for frame in BrowserPreview(run).stream(connected)]
    assert len(frames) == 1
    assert b"Content-Type: image/png" in frames[0]
    assert frames[0].endswith(b"final-frame\r\n")


async def test_disconnected_view_never_captures_or_closes_driver():
    async def disconnected():
        return True
    def fail_capture():
        raise AssertionError("A disconnected viewer must not request a frame")
    run = SimpleNamespace(request=SimpleNamespace(target="browser"), driver=None, screenshot=fail_capture)
    assert [frame async for frame in BrowserPreview(run).stream(disconnected)] == []
