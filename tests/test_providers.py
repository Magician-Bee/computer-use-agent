import json
import asyncio

import httpx
import pytest

from server.providers import (OLLAMA_FINAL_PREFILL, ProviderError, _needs_ollama_final_prefill,
                              build_request, complete, next_action, observation_text, ollama_action_schema, parse_action)
from server.schemas import ModelConfig

IMAGE = "data:image/png;base64,aGVsbG8="


@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "ollama", "custom"])
def test_text_only_never_serializes_screenshot(provider):
    config = ModelConfig(provider=provider, model="test", base_url="http://localhost:1234/v1", api_key="test-key", vision=False)
    prompt = observation_text("task", {"image": IMAGE, "width": 100, "text": "safe", "secret_internal": "do-not-send"}, [])
    url, headers, body = build_request(config, prompt, IMAGE)
    encoded = json.dumps(body)
    assert "aGVsbG8" not in encoded
    assert "do-not-send" not in encoded
    assert "test-key" not in encoded and "test-key" not in url
    assert "safe" in encoded


@pytest.mark.parametrize("provider,field", [("openai", "image_url"), ("anthropic", "base64"), ("gemini", "inlineData"), ("ollama", "images")])
def test_vision_uses_provider_image_encoding(provider, field):
    config = ModelConfig(provider=provider, model="test", api_key="test-key", vision=True)
    _, _, body = build_request(config, "task", IMAGE)
    assert field in json.dumps(body)
    assert "aGVsbG8=" in json.dumps(body)


def test_action_json_rejects_unsafe_or_unbounded_operations():
    for raw in [
        '{"type":"shell","text":"echo bad"}',
        '{"type":"navigate","url":"file:///tmp/private"}',
        '{"type":"click","x":-1,"y":20}',
        '{"type":"wait","seconds":600}',
        '{"type":"done","text":"ok","script":"doSomething()"}',
        '{"type":"done","text":"ok"}{"type":"click","x":1,"y":1}',
    ]:
        with pytest.raises(ProviderError):
            parse_action(raw)
    assert parse_action('```json\n{"type":"click","target":"ocr_2"}\n```').target == "ocr_2"


def test_action_validation_feedback_is_useful_without_echoing_values_or_unknown_keys():
    with pytest.raises(ProviderError) as exc:
        parse_action('{"secret-as-field-name":"secret-as-input-value","action":"secret-action"}')
    message = str(exc.value)
    assert "type: missing" in message
    assert "extra_forbidden" in message
    assert "secret" not in message
    with pytest.raises(ProviderError) as exc:
        parse_action('{"type":"click","target":"invalid / private-token"}')
    assert "target: string_pattern_mismatch" in str(exc.value)
    assert "private-token" not in str(exc.value)


async def test_api_errors_do_not_disclose_credentials(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            return httpx.Response(401, json={"error": "secret-key-must-not-leak"})
    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    with pytest.raises(ProviderError) as error:
        await next_action(ModelConfig(provider="openai", api_key="secret-key-must-not-leak"), "task", {}, [])
    assert "secret-key" not in str(error.value)
    assert "401" in str(error.value)


def test_endpoint_validation_prevents_credential_urls():
    for value in ["https://user:password@api.example.com/v1", "http://remote.example.com", "https://api.example.com?key=secret"]:
        with pytest.raises(ValueError):
            ModelConfig(provider="custom", base_url=value)


QWEN_THINKING_METADATA = {
    "details": {"family": "qwen3vl"},
    "modelfile": "FROM local-weights\nRENDERER qwen3-vl-thinking\nPARSER qwen3-vl-thinking\n",
}


@pytest.mark.parametrize("metadata", [None, [], {}, {"details": None},
    {"details": {"family": "minicpmv"}, "modelfile": QWEN_THINKING_METADATA["modelfile"]},
    {"details": {"family": "qwen3vl"}, "modelfile": "RENDERER qwen3-vl\nPARSER qwen3-vl"},
    {"details": {"family": "qwen3vl"}, "modelfile": ["qwen3-vl-thinking"]},
])
def test_ollama_prefill_only_matches_confirmed_thinking_renderer(metadata):
    assert not _needs_ollama_final_prefill(metadata)
    assert _needs_ollama_final_prefill(QWEN_THINKING_METADATA)


@pytest.mark.parametrize("vision", [False, True])
async def test_qwen_compatibility_prefills_final_channel_without_supplying_actions(monkeypatch, vision):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/api/show"):
                return httpx.Response(200, json=QWEN_THINKING_METADATA)
            return httpx.Response(200, json={"message": {
                "content": '{"reason":"Click the observed button","type":"click","target":"ocr_9"}',
                "thinking": '{"type":"click","target":"wrong-target"}',
            }})

    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    config = ModelConfig(provider="ollama", model="user-alias", base_url="http://127.0.0.1:11434/v1", vision=vision)
    action = await next_action(config, "click visible button", {"image": IMAGE, "elements": [{"id": "ocr_9"}]}, [])
    assert action.target == "ocr_9"
    assert calls[0][0] == "http://127.0.0.1:11434/api/show"
    assert calls[0][1]["json"] == {"model": "user-alias"}
    body = calls[1][1]["json"]
    assert body["messages"][-1] == {"role": "assistant", "content": OLLAMA_FINAL_PREFILL}
    assert "type" not in OLLAMA_FINAL_PREFILL and "target" not in OLLAMA_FINAL_PREFILL
    assert body["think"] is False and "oneOf" in body["format"]
    assert ("aGVsbG8=" in json.dumps(body)) is vision


@pytest.mark.parametrize("metadata,status", [({}, 404), ([], 200), ({"details": {"family": "minicpmv"}}, 200)])
async def test_missing_or_other_ollama_metadata_keeps_standard_chat(monkeypatch, metadata, status):
    requests = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            requests.append(kwargs["json"])
            if url.endswith("/show"):
                return httpx.Response(status, json=metadata)
            return httpx.Response(200, json={"message": {"content": '{"type":"done","text":"verified"}'}})

    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    assert parse_action(await complete(ModelConfig(provider="ollama"), "task")).type == "done"
    assert [m["role"] for m in requests[-1]["messages"]] == ["system", "user"]


async def test_thinking_only_response_is_never_promoted_to_an_action(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            if url.endswith("/show"):
                return httpx.Response(200, json=QWEN_THINKING_METADATA)
            return httpx.Response(200, json={"message": {"content": "", "thinking": '{"type":"click","target":"secret-from-thinking"}'}})

    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    with pytest.raises(ProviderError) as exc:
        await next_action(ModelConfig(provider="ollama"), "task", {}, [])
    assert "secret-from-thinking" not in str(exc.value)


async def test_stop_cancellation_interrupts_ollama_capability_probe(monkeypatch):
    started = asyncio.Event()
    closed = asyncio.Event()

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.set()
        async def post(self, url, **kwargs):
            assert url.endswith("/show")
            started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    task = asyncio.create_task(complete(ModelConfig(provider="ollama"), "task"))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_ollama_schema_avoids_large_string_grammar_but_runtime_limits_remain():
    schema = ollama_action_schema()
    assert "maxLength" not in json.dumps(schema)
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["type"]
    assert "shell" not in schema["properties"]["type"]["enum"]
    assert schema["properties"]["target"]["anyOf"][0]["pattern"] == "^[A-Za-z0-9_-]+$"
    with pytest.raises(ProviderError, match="string_too_long"):
        parse_action(json.dumps({"type": "type", "text": "x" * 12001}))


async def test_ollama_schema_grammar_failure_retries_json_once(monkeypatch):
    formats = []

    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, **kwargs):
            if url.endswith("/show"):
                return httpx.Response(404)
            formats.append(kwargs["json"]["format"])
            if len(formats) == 1:
                return httpx.Response(400, json={"error": "failed to parse grammar: private-data-must-not-leak"})
            return httpx.Response(200, json={"message": {"content": '{"type":"done","text":"verified"}'}})

    monkeypatch.setattr("server.providers.httpx.AsyncClient", Client)
    assert parse_action(await complete(ModelConfig(provider="ollama"), "task")).type == "done"
    assert isinstance(formats[0], dict)
    assert formats[1] == "json"


def test_schema_grounds_ids_without_choosing_an_action_or_task_answer():
    observation = {"target": "browser", "elements": [
        {"id": "e_current", "role": "textbox", "value": "screen-secret"},
        {"id": "ocr_8", "text": "screen-secret"}, {"id": "bad id"},
    ]}
    schema = ollama_action_schema(observation)
    assert schema["properties"]["target"]["anyOf"][0]["enum"] == ["e_current", "ocr_8"]
    assert "screen-secret" not in json.dumps(schema)
    assert "open_app" not in schema["properties"]["type"]["enum"]
    assert {"click", "type", "select_option", "done"} <= set(schema["properties"]["type"]["enum"])
    desktop = ollama_action_schema({"target": "desktop", "elements": []})
    assert desktop["properties"]["target"] == {"type": "null"}
    assert "open_app" in desktop["properties"]["type"]["enum"]
    assert "select_option" not in desktop["properties"]["type"]["enum"]


async def test_outdated_target_is_rejected_even_if_server_ignores_grammar(monkeypatch):
    async def fake_complete(*args, **kwargs):
        assert "e_current" in json.dumps(kwargs["action_schema"])
        assert "e_old" not in json.dumps(kwargs["action_schema"])
        return '{"reason":"Click the button","type":"click","target":"e_old"}'

    monkeypatch.setattr("server.providers.complete", fake_complete)
    with pytest.raises(ProviderError, match="target"):
        await next_action(ModelConfig(provider="ollama"), "task", {"elements": [{"id": "e_current"}]}, [])


def test_grounded_schema_caps_the_same_250_elements_as_model_observation():
    schema = ollama_action_schema({"elements": [{"id": f"e{i}"} for i in range(300)]})
    ids = schema["properties"]["target"]["anyOf"][0]["enum"]
    assert len(ids) == 250 and "e249" in ids and "e250" not in ids
