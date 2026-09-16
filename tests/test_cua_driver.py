"""Cua adapter tests use injected messages, never a native app or desktop input."""
import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from server.cua_driver import CuaDriver, _result_parts
from server.drivers import ActionInterrupted, DriverAbort


def snapshot(**overrides):
    png = io.BytesIO()
    Image.new('RGB', (800, 600), 'white').save(png, format='PNG')
    state = {'pid': 123, 'window_id': 456, 'screenshot_width': 800, 'screenshot_height': 600,
             'screenshot_frame_valid': True, 'window_bounds': {'x': 100, 'y': 50, 'width': 400, 'height': 300},
             'elements': [{'element_token': 'opaque', 'role': 'AXButton', 'label': 'Save', 'actions': ['AXPress'],
                           'frame': {'x': 150, 'y': 100, 'w': 100, 'h': 30}}]}
    state.update(overrides)
    return {'structuredContent': state, 'content': [{'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(png.getvalue()).decode()}]}


class FakeTransport:
    def __init__(self):
        self.calls, self.closed, self.interrupted = [], False, False
        self.result = snapshot()
    async def start(self):
        self.closed = False
    async def close(self):
        self.closed = True
    def interrupt(self):
        self.interrupted = True
    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def action(kind='click', **kwargs):
    return SimpleNamespace(**({'type': kind, 'target': 'cua_1_1', 'x': None, 'y': None,
                              'button': 'left', 'key': None, 'text': None, 'seconds': 0} | kwargs))


@pytest.mark.parametrize('pid,wid', [(None,None),(True,456),(123,False),(0,456),(123,-1)])
async def test_requires_explicit_valid_window_identity(pid,wid):
    fake = FakeTransport()
    driver = CuaDriver(pid,wid,_transport=fake)
    with pytest.raises(ValueError):
        await driver.start()
    assert not fake.calls


async def test_exact_window_retina_mapping_and_no_implicit_input():
    fake = FakeTransport()
    driver = CuaDriver(123,456,_transport=fake)
    await driver.start()
    observation = await driver.observe()
    item = observation['elements'][0]
    assert (item['x'], item['y'], item['width'], item['height']) == (200,130,200,60)
    assert 'element_token' not in observation['text']
    assert observation['supported_actions'] == []
    assert observation['key_capabilities']['supported_keys'] == []
    with pytest.raises(ValueError, match='尚未通過'):
        await driver.execute(action())
    assert [name for name,_ in fake.calls] == ['get_window_state']


async def test_failed_observation_invalidates_earlier_tokens():
    fake = FakeTransport()
    driver = CuaDriver(123,456,enabled_actions=['click'],_transport=fake)
    await driver.observe()
    fake.result = snapshot(pid=999)
    with pytest.raises(DriverAbort):
        await driver.observe()
    with pytest.raises(ValueError, match='原生元素'):
        await driver.execute(action())
    assert all(name == 'get_window_state' for name,_ in fake.calls)


@pytest.mark.parametrize('change', [{'screenshot_frame_valid':False}, {'screenshot_width':801}, {'window_bounds':{'x':0,'y':0,'width':400,'height':600}}])
async def test_refuses_unproven_capture_transform(change):
    fake = FakeTransport(); fake.result = snapshot(**change)
    with pytest.raises(RuntimeError):
        await CuaDriver(123,456,_transport=fake).observe()


async def test_candidate_exact_native_token_and_no_pixel_or_hotkey_fallback():
    fake = FakeTransport()
    driver = CuaDriver(123,456,enabled_actions=['click','key'],_transport=fake)
    await driver.observe()
    with pytest.raises(ValueError):
        await driver.execute(action(x=200,y=130))
    fake.result = {'structuredContent':{'path':'ax','effect':'unverifiable'}, 'content':[]}
    await driver.execute(action())
    name,args = fake.calls[-1]
    assert name == 'click'
    assert args['pid'] == 123 and args['window_id'] == 456 and args['element_token'] == 'opaque'
    assert args['delivery_mode'] == 'background' and 'x' not in args and 'y' not in args
    with pytest.raises(ValueError):
        await driver.execute(action())  # consumed, regardless of successful tool return
    assert len(fake.calls) == 2


async def test_pause_stops_transport_not_only_waiting_future():
    fake = FakeTransport()
    pending = asyncio.Event()
    async def block(*args):
        pending.set(); await asyncio.Event().wait()
    fake.call = block
    driver = CuaDriver(123,456,_transport=fake)
    task = asyncio.create_task(driver.observe())
    await pending.wait()
    driver.interrupt_action()
    with pytest.raises(ActionInterrupted):
        await task
    assert fake.interrupted
    await driver.close()
    assert fake.closed


def test_no_fabricated_state_from_text_only_errors():
    with pytest.raises(RuntimeError):
        _result_parts({'content':[{'type':'text','text':'All good, button id=1'}]})
    with pytest.raises(RuntimeError):
        _result_parts({'isError':True,'structuredContent':{'success':True},'content':[]})
