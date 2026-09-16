"""Headless Chromium tests observe real pointer events; no native input is sent."""
import asyncio
import base64
import io
import math
from types import SimpleNamespace

import pytest
from PIL import Image

from server.drivers import ActionInterrupted, BrowserDriver
from server.motion import MotionPolicy, trajectory


def action(kind='click', **kwargs):
    return SimpleNamespace(**({'type': kind, 'target': None, 'x': None, 'y': None,
        'end_x': None, 'end_y': None, 'button': 'left', 'text': None, 'duration': 1,
        'url': None, 'key': None, 'seconds': 0} | kwargs))


HTML = '''<style>body{margin:0;background:white}button{position:absolute;left:600px;top:300px;width:120px;height:60px}</style>
<button id="target">Save</button><script>
window.events=[]; window.saved=0;
for (const kind of ['pointermove','pointerdown','pointerup','click','dblclick']) {
 document.addEventListener(kind,e=>events.push({kind,x:e.clientX,y:e.clientY,buttons:e.buttons,time:performance.now(),target:e.target.id}),true);
}
target.addEventListener('click',()=>saved++);
</script>'''


@pytest.fixture
async def browser():
    driver = BrowserDriver()
    await driver.start()
    await driver._page.set_content(HTML)
    await driver.observe()
    try:
        yield driver
    finally:
        await driver.close()


def test_trajectory_has_bounded_minimum_jerk_profile():
    points = trajectory((0,0),(1000,500),MotionPolicy())
    assert (points[0].x,points[0].y,points[0].seconds) == (0,0,0)
    assert (points[-1].x,points[-1].y,points[-1].seconds) == (1000,500,1.2)
    assert all(0 <= p.x <= 1000 and 0 <= p.y <= 500 for p in points)
    distances = [math.dist((a.x,a.y),(b.x,b.y)) for a,b in zip(points,points[1:])]
    assert max(distances[:4]) < max(distances[len(distances)//2-2:len(distances)//2+2])
    assert max(distances[-4:]) < max(distances[len(distances)//2-2:len(distances)//2+2])


async def test_real_click_moves_continuously_then_clicks_and_screenshot_shows_executor_cursor(browser):
    target = next(e['id'] for e in browser._observation['elements'] if e['text']=='Save')
    await browser.execute(action(target=target))
    events = await browser._page.evaluate('events')
    moves = [e for e in events if e['kind']=='pointermove']
    assert len(moves) >= 15
    assert moves[0]['x']==0 and moves[-1]['x']==660 and moves[-1]['y']==330
    assert moves[-1]['time']-moves[0]['time'] >= 250
    assert events[-1]['kind']=='click' and events[-1]['target']=='target'
    assert await browser._page.evaluate('saved')==1
    observed = await browser.observe()
    assert observed['cursor']['x']==660 and observed['cursor']['y']==330
    assert observed['cursor']['samples_sent']>=15
    assert observed['physical_input_untouched'] is True
    assert observed['perception_exclusions']==[{'kind':'executor_cursor','bbox':[660,330,22,30]}]
    assert len(observed['elements'])==1 and 'computeruse-cursor' not in observed['text']
    image = Image.open(io.BytesIO(base64.b64decode(observed['image'].split(',',1)[1]))).convert('RGB')
    purple = sum(1 for r,g,b in image.crop((660,330,682,360)).getdata() if r>90 and b>150 and g<130)
    assert purple>30


async def test_moving_target_is_rejected_before_mouse_down(browser):
    target = next(iter(browser._handles))
    await browser._page.evaluate("document.addEventListener('pointermove',e=>{if(e.clientX>200)target.style.left='900px'})")
    with pytest.raises(ValueError, match='改變位置'):
        await browser.execute(action(target=target))
    assert await browser._page.evaluate("events.filter(e=>e.kind==='pointerdown').length")==0
    assert await browser._page.evaluate('saved')==0


async def test_pause_mid_trajectory_sends_no_click_and_can_resume(browser):
    task = asyncio.create_task(browser.execute(action(x=900,y=450)))
    await browser._page.wait_for_function("events.filter(e=>e.kind==='pointermove').length>=5")
    browser.interrupt_action()
    with pytest.raises(ActionInterrupted):
        await task
    assert await browser._page.evaluate("events.some(e=>e.kind==='pointerdown')") is False
    assert browser._pointer(browser._page).position != (900,450)
    await browser.resume_actions()
    await browser.observe()
    await browser.execute(action(x=660,y=330))
    assert await browser._page.evaluate('saved')==1


@pytest.mark.parametrize('cancel', [False,True])
async def test_interrupted_drag_releases_real_page_button(browser,cancel):
    task = asyncio.create_task(browser.execute(action('drag',x=20,y=50,end_x=950,end_y=500,duration=1.1)))
    await browser._page.wait_for_function("events.filter(e=>e.kind==='pointermove' && e.buttons===1).length>=4")
    if cancel:
        task.cancel()
        expected = asyncio.CancelledError
    else:
        browser.interrupt_action()
        expected = ActionInterrupted
    with pytest.raises(expected):
        await task
    events = await browser._page.evaluate('events')
    assert sum(e['kind']=='pointerdown' for e in events)==1
    assert sum(e['kind']=='pointerup' for e in events)==1
    assert browser._pointer(browser._page).metadata()['pressed_buttons']==[]
    await browser.resume_actions()
    await browser.execute(action('move',x=200,y=100))
    assert (await browser._page.evaluate("events.filter(e=>e.kind==='pointermove').at(-1)"))['buttons']==0


async def test_each_tab_keeps_its_own_pointer(browser):
    first = browser._page
    await browser.execute(action('move',x=300,y=150))
    await browser.execute(action('new_tab'))
    second = browser._page
    await browser.observe()
    await browser.execute(action('move',x=60,y=80))
    assert browser._pointer(first).position==(300,150)
    assert browser._pointer(second).position==(60,80)
    await browser.execute(action('switch_tab',text='tab_1'))
    observation = await browser.observe()
    assert (observation['cursor']['x'],observation['cursor']['y'])==(300,150)


async def test_double_click_uses_balanced_events_and_native_dblclick(browser):
    await browser.execute(action('double_click',target=next(iter(browser._handles))))
    events = await browser._page.evaluate('events')
    assert sum(e['kind']=='pointerdown' for e in events)==2
    assert sum(e['kind']=='pointerup' for e in events)==2
    assert sum(e['kind']=='dblclick' for e in events)==1
    assert browser._pointer(browser._page).metadata()['pressed_buttons']==[]


async def test_visible_iframe_target_receives_actual_click(browser):
    await browser._page.set_content('''<iframe style="position:absolute;left:200px;top:100px;width:600px;height:400px" srcdoc="<button onclick='document.body.dataset.clicked=1'>Frame save</button>"></iframe>''')
    observation = await browser.observe()
    target = next(e['id'] for e in observation['elements'] if e['text']=='Frame save')
    await browser.execute(action(target=target))
    assert await browser._page.frames[1].evaluate('document.body.dataset.clicked')=='1'


async def test_close_drains_drag_release_before_destroying_context(browser):
    releases = []
    await browser._page.expose_function('record_transport_event', lambda kind: releases.append(kind))
    await browser._page.evaluate("for(const k of ['pointerdown','pointerup'])document.addEventListener(k,e=>record_transport_event(k))")
    task = asyncio.create_task(browser.execute(action('drag',x=20,y=50,end_x=950,end_y=500,duration=1.1)))
    await browser._page.wait_for_function("events.filter(e=>e.kind==='pointermove' && e.buttons===1).length>=4")
    await browser.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert releases==['pointerdown','pointerup']
    assert browser._context is None and browser._actions==set()
