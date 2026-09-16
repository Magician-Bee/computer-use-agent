"""Public-package and frontend-provider default regressions; no external requests."""
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
import pytest
from server.providers import build_request, DEFAULT_URLS
from server.schemas import ModelConfig

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('provider,suffix', [('openai', '/chat/completions'), ('anthropic', '/messages'), ('gemini', '/models/test-model:generateContent')])
def test_frontend_defaults_generate_versioned_provider_urls(provider, suffix):
    source = (ROOT / 'frontend/src/Settings.tsx').read_text(encoding='utf-8')
    match = re.search(r'\b' + provider + r": \{ base_url: '([^']+)'", source)
    assert match is not None
    assert match[1] == DEFAULT_URLS[provider]
    url, _, _ = build_request(ModelConfig(provider=provider, model='test-model', api_key='synthetic-test-key', base_url=match[1]), 'fixture')
    assert url == DEFAULT_URLS[provider] + suffix

def test_original_code_has_explicit_license():
    license_text = (ROOT / 'LICENSE').read_text(encoding='utf-8')
    assert license_text.startswith('MIT License')
    assert 'Magician-Bee' in license_text
    assert 'Third-party' in (ROOT / 'THIRD_PARTY_NOTICES.md').read_text(encoding='utf-8')

def test_public_readme_links_resolve():
    source = (ROOT / 'README.md').read_text(encoding='utf-8')
    source = re.sub(r'```[\s\S]*?```', '', source)
    for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)', source):
        parsed = urlsplit(target)
        if not parsed.scheme and parsed.path:
            assert (ROOT / unquote(parsed.path)).exists(), 'Missing public documentation link'

def test_workspace_does_not_load_remote_fonts():
    html = (ROOT / 'frontend/index.html').read_text(encoding='utf-8') + (ROOT / 'frontend/src/styles.css').read_text(encoding='utf-8')
    assert 'fonts.googleapis.com' not in html
    assert 'fonts.gstatic.com' not in html

def test_benchmark_uses_portable_home():
    source = (ROOT / 'scripts/benchmark_hermes.py').read_text(encoding='utf-8')
    assert 'default=Path.home()' in source
    assert '/Users/' not in source
