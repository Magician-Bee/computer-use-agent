"""Role alone cannot distinguish editable ARIA comboboxes from native selects.

Neutral local fixtures; no site selectors, model calls, or native desktop input.
"""
import json

import pytest
from jsonschema import Draft202012Validator

from server.action_space import build_action_space, prepare_model_observation
from server.drivers import BrowserDriver
from server.providers import observation_text
from server.schemas import Action


@pytest.mark.parametrize('tag', ['input', 'textarea'])
async def test_editable_combobox_and_native_select_have_distinct_real_capabilities(tag):
    driver = BrowserDriver(headless=True)
    try:
        await driver.start()
        await driver._page.set_content(f'''<label>Lookup <{tag} role="combobox" aria-label="Lookup"></{tag}></label>
            <label>Category <select aria-label="Category"><option>Alpha</option><option>Beta</option></select></label>''')
        observed = await driver.observe()
        observed['target'] = 'browser'  # Run.observe supplies executor kind.
        prepared = prepare_model_observation(observed)
        prompt = json.loads(observation_text('Look up the requested term', prepared, []))
        nodes = prompt['current_observation_UNTRUSTED']['elements']
        editable = next(e for e in nodes if e['text'] == 'Lookup')
        select = next(e for e in nodes if e['text'] == 'Category')
        assert editable['role'] == select['role'] == 'combobox'
        assert not isinstance(editable.get('options'), list)
        assert isinstance(select['options'], list)
        contract = Draft202012Validator(build_action_space(prepared))
        typing = {'reason': 'Enter the requested term', 'type': 'type', 'target': editable['id'], 'text': '機械手臂'}
        choosing = {'reason': 'Choose a category', 'type': 'select_option', 'target': select['id'], 'text': 'Beta'}
        assert contract.is_valid(typing)
        assert contract.is_valid(choosing)
        assert not contract.is_valid({**typing, 'target': select['id']})
        assert not contract.is_valid({**choosing, 'target': editable['id']})
        # No focusing click is needed; the real driver focuses and fills either tag.
        await driver.execute(Action(**typing))
        readback = await driver.observe()
        assert next(e for e in readback['elements'] if e['text'] == 'Lookup')['value'] == '機械手臂'
        await driver.execute(Action(**choosing))
        readback = await driver.observe()
        assert next(e for e in readback['elements'] if e['text'] == 'Category')['value'] == 'Beta'
    finally:
        await driver.close()
