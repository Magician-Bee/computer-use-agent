#!/usr/bin/env python3
"""Reproduce live JPEG frames during real headless browser pointer movement.

This is a scripted driver smoke, never a model benchmark. No native window,
physical mouse/keyboard, model endpoint or external website is used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image
from benchmarks.provenance import provenance
from server.agent import Run
from server.drivers import BrowserDriver
from server.motion import MotionPolicy
from server.preview import BrowserPreview
from server.schemas import Action, ModelConfig, RunRequest

HTML = '''<!doctype html><title>Live independent cursor proof</title>
<style>body{margin:64px;background:#f5f6fc;color:#17223b;font:23px system-ui}h1{font-size:40px}p{max-width:760px;line-height:1.6}button{position:absolute;left:970px;top:510px;width:220px;height:86px;border:0;border-radius:16px;background:#1f6feb;color:white;font:24px system-ui}.track{position:absolute;left:80px;top:620px;font-size:17px;color:#536078}</style>
<h1>Live browser cursor verification</h1><p>JPEG frames below come from the actual isolated Chromium page while its own pointer moves. This scripted transport check calls no model.</p><button id="save">Save fixture</button><div class="track">Headless Chromium · independent browser input · actual streamed frames</div>
<script>window.transportEvents=[];window.fixtureSaved=false;
for(const kind of ['pointermove','pointerdown','pointerup','click'])document.addEventListener(kind,e=>transportEvents.push({kind,x:e.clientX,y:e.clientY,buttons:e.buttons,time:performance.now()}),true);
save.addEventListener('click',()=>{fixtureSaved=true;save.textContent='Saved';});</script>'''


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def decode_mjpeg_part(chunk: bytes) -> bytes:
    header, payload = chunk.split(b'\r\n\r\n', 1)
    assert header.startswith(b'--computeruse-frame\r\n')
    assert b'Content-Type: image/jpeg' in header
    size = int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.startswith(b'Content-Length:')))
    assert payload.endswith(b'\r\n')
    raw = payload[:-2]
    assert len(raw) == size
    return raw


def purple_cursor_bbox(raw: bytes) -> list[int] | None:
    """Locate the rendered purple SVG in this known, non-purple fixture.

    Pixel evidence is independent of the executor's position metadata; fixture
    colors deliberately contain no other purple content.
    """
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == 'JPEG' and image.size == (1280,800)
        rgb = image.convert('RGB')
        mask = Image.merge('RGB',rgb.split())
        pixels = mask.load()
        min_x,min_y,max_x,max_y = 1280,800,-1,-1
        for y in range(800):
            for x in range(1280):
                r,g,b = pixels[x,y]
                if b > 165 and r > 75 and r-g > 25 and b-r > 35 and g < 155:
                    min_x,min_y,max_x,max_y = min(min_x,x),min(min_y,y),max(max_x,x),max(max_y,y)
        return [min_x,min_y,max_x-min_x+1,max_y-min_y+1] if max_x >= 0 else None


async def diagnose(output: Path) -> dict:
    output.mkdir(parents=True,exist_ok=False)
    driver = BrowserDriver(headless=True)
    driver.configure_motion(MotionPolicy(min_duration=1.8,max_duration=1.8))
    run = Run(RunRequest(task='Scripted browser transport check',target='browser'),
              ModelConfig(provider='demo',model='scripted-driver-smoke',perception='auto'), 'about:blank')
    run.driver = driver
    collector = None
    frames = []
    first_frame = asyncio.Event()
    stop = asyncio.Event()
    started = time.monotonic()
    try:
        await driver.start()
        await driver._page.set_content(HTML)
        observation = await run.observe()
        frozen_hash = fingerprint(observation)
        selected_page = driver._page
        observed_target = next(e['id'] for e in observation['elements'] if e['text']=='Save fixture')
        preview = BrowserPreview(run)
        run.status = 'running'
        async def disconnected():
            return stop.is_set()
        async def collect():
            async for chunk in preview.stream(disconnected):
                raw = decode_mjpeg_part(chunk)
                frames.append({'jpeg':raw,'elapsed':round(time.monotonic()-started,4),
                    'sha256':hashlib.sha256(raw).hexdigest(),
                    'cursor_at_receive':driver._pointer(selected_page).metadata(),
                    'observation_unchanged': run.observation is observation and driver._observation is observation
                        and fingerprint(run.observation)==frozen_hash,
                    'selected_page_unchanged':driver._page is selected_page})
                first_frame.set()
        collector = asyncio.create_task(collect())
        await asyncio.wait_for(first_frame.wait(),timeout=5)
        action_started = time.monotonic()
        await driver.execute(Action(type='click',target=observed_target))
        action_seconds = time.monotonic()-action_started
        await asyncio.sleep(.42)  # Allow one uncached final frame after actual mouse-up.
        stop.set()
        await asyncio.wait_for(collector,timeout=5)
        events = await selected_page.evaluate('transportEvents')
        saved = await selected_page.evaluate('fixtureSaved')
        final_cursor = driver._pointer(selected_page).metadata()
        for item in frames:
            item['pixel_cursor_bbox'] = await asyncio.to_thread(purple_cursor_bbox,item['jpeg'])
        positions = [item['pixel_cursor_bbox'] for item in frames if item['pixel_cursor_bbox']]
        unique_positions = sorted({tuple(box[:2]) for box in positions})
        between = [xy for xy in unique_positions if 100 < xy[0] < final_cursor['x']-100]
        moves = [event for event in events if event['kind']=='pointermove']
        checks = {'received_at_least_six_jpeg_frames':len(frames)>=6,
            'at_least_five_distinct_jpeg_frames':len({f['sha256'] for f in frames})>=5,
            'at_least_three_intermediate_cursor_pixel_positions':len(between)>=3,
            'smooth_pointer_events':len(moves)>=30 and moves[-1]['time']-moves[0]['time']>=1500,
            'actual_click_and_saved_oracle': saved is True and sum(e['kind']=='click' for e in events)==1,
            'balanced_mouse_buttons':sum(e['kind']=='pointerdown' for e in events)==sum(e['kind']=='pointerup' for e in events)==1,
            'model_observation_unchanged':all(f['observation_unchanged'] for f in frames) and fingerprint(run.observation)==frozen_hash,
            'selected_page_unchanged':all(f['selected_page_unchanged'] for f in frames),
            'all_frames_contain_real_cursor_pixels':len(positions)==len(frames),
            'final_cursor_matches_pixels': bool(positions) and abs(positions[-1][0]-final_cursor['x'])<10 and abs(positions[-1][1]-final_cursor['y'])<10}
        middle = min(range(len(frames)),key=lambda i: abs((frames[i]['pixel_cursor_bbox'] or [0])[0]-final_cursor['x']/2))
        for label,index in [('start',0),('middle',middle),('end',len(frames)-1)]:
            filename=label+'.jpg'
            (output/filename).write_bytes(frames[index]['jpeg'])
            frames[index].setdefault('saved_as',[]).append(filename)
        report = {'planner_mode':'scripted_driver_smoke','model':None,'model_calls':0,
            'headless':True,'input_transport':'playwright_page_mouse','preview_transport':'BrowserPreview.stream multipart JPEG',
            'production_browser_driver':True,'native_desktop_used':False,'ui_frontend_validated':False,
            'elapsed_seconds':round(time.monotonic()-started,3),'click_with_motion_seconds':round(action_seconds,3),
            'configured_motion_duration_seconds':1.8,'frame_count':len(frames),
            'unique_jpeg_count':len({f['sha256'] for f in frames}),'unique_cursor_pixel_positions':unique_positions,
            'model_observation_sha256_before':frozen_hash,'model_observation_sha256_after':fingerprint(run.observation),
            'checks':checks,'passed':all(checks.values()),'saved_oracle':saved,'cursor':final_cursor,
            'frames':[{k:v for k,v in f.items() if k!='jpeg'} for f in frames],
            'actual_pointer_events':events,'provenance':provenance(ROOT),
            'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        return report
    finally:
        stop.set()
        if collector is not None and not collector.done():
            collector.cancel()
            await asyncio.gather(collector,return_exceptions=True)
        await driver.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/browser-live-preview-proof')
    args=parser.parse_args()
    report=asyncio.run(diagnose(args.output.resolve()))
    print(json.dumps({k:report[k] for k in ('passed','frame_count','unique_jpeg_count','click_with_motion_seconds','checks')},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
