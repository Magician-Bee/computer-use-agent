#!/usr/bin/env python3
"""Bounded real-HTTP MJPEG smoke with an owned uvicorn and headless monitor.

The server uses the production API, driver and preview unchanged. This launcher
adds a test-only read-only fixture oracle during driver close and monitor page.
It traps any model call. Neither native input nor the user's port 8765 is used.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

MONITOR_HTML = '''<!doctype html><title>HTTP MJPEG monitor proof</title><link rel="icon" href="data:,">
<style>body{margin:0;background:#101217;color:#eee;font:16px system-ui}header{padding:14px}img{display:block;width:1280px;height:800px}</style>
<header>Real HTTP MJPEG decode · headless monitor · scripted self-test</header><img id="live" src="VIEW_URL"><canvas id="sample" hidden width="1280" height="800"></canvas>
<script>window.decodedFrames=[];window.monitorErrors=[];let last='';
live.addEventListener('error',()=>monitorErrors.push('img decode/load error'));
setInterval(()=>{if(live.naturalWidth!==1280)return;try{const c=sample.getContext('2d',{willReadFrequently:true});c.drawImage(live,0,0,1280,800);const d=c.getImageData(0,0,1280,800).data;let minX=1280,minY=800,maxX=-1,maxY=-1;for(let i=0;i<d.length;i+=4){const r=d[i],g=d[i+1],b=d[i+2];if(b>165&&r>75&&r-g>25&&b-r>35&&g<155){const p=i/4,x=p%1280,y=Math.floor(p/1280);minX=Math.min(minX,x);minY=Math.min(minY,y);maxX=Math.max(maxX,x);maxY=Math.max(maxY,y);}}const box=maxX>=0?[minX,minY,maxX-minX+1,maxY-minY+1]:null;const key=JSON.stringify(box);if(key!==last){last=key;decodedFrames.push({time:performance.now(),naturalWidth:live.naturalWidth,naturalHeight:live.naturalHeight,cursorPixels:box,jpeg:sample.toDataURL('image/jpeg',.8)});}}catch(e){monitorErrors.push(String(e));}},80);</script>'''


def serve(port: int, fd: int):
    """Test-only launcher; all production files remain unchanged."""
    import uvicorn
    from fastapi.responses import HTMLResponse
    app_module=importlib.import_module('server.app')
    agent_module=importlib.import_module('server.agent')
    original_driver=agent_module.BrowserDriver
    oracles={}
    calls={'model_calls':0}
    async def prohibit_model(*args,**kwargs):
        calls['model_calls']+=1
        raise AssertionError('HTTP preview proof must not invoke a model')
    agent_module.next_action=prohibit_model
    class OracleBrowserDriver(original_driver):
        async def close(self):
            page=getattr(self,'_page',None)
            if page is not None and not page.is_closed() and page.url==f'http://127.0.0.1:{port}/demo':
                try:
                    oracles[id(self)]=await page.evaluate('''() => ({source:'actual_demo_DOM_read_on_close',url:location.href,name:document.querySelector('#name')?.value,result:document.querySelector('#result')?.textContent,result_hidden:document.querySelector('#result')?.hidden})''')
                except Exception as exc:
                    oracles[id(self)]={'error':type(exc).__name__}
            await super().close()
    agent_module.BrowserDriver=OracleBrowserDriver
    @app_module.app.get('/__proof/monitor/{run_id}',response_class=HTMLResponse)
    async def monitor(run_id:str):
        app_module.find_run(run_id)
        return MONITOR_HTML.replace('VIEW_URL',f'/api/sessions/{run_id}/view')
    @app_module.app.get('/__proof/oracle/{run_id}')
    async def oracle(run_id:str):
        run=app_module.find_run(run_id)
        return {'oracle':oracles.get(id(run.driver)),**calls,'task_done':bool(run.task and run.task.done())}
    uvicorn.run(app_module.app,fd=fd,host='127.0.0.1',port=port,log_level='warning',access_log=False)


def parse_parts(buffer:bytes):
    output=[]
    while True:
        offset=buffer.find(b'--computeruse-frame\r\n')
        if offset<0:
            break
        header_end=buffer.find(b'\r\n\r\n',offset)
        if header_end<0:
            break
        header=buffer[offset:header_end]
        headers={line.split(b':',1)[0].lower():line.split(b':',1)[1].strip() for line in header.split(b'\r\n')[1:] if b':' in line}
        size=int(headers[b'content-length'])
        start=header_end+4
        if len(buffer)<start+size+2:
            break
        assert buffer[start+size:start+size+2]==b'\r\n'
        output.append((buffer[start:start+size],headers[b'content-type'].decode()))
        buffer=buffer[start+size+2:]
    return output,buffer


async def diagnose(output:Path):
    import httpx
    from PIL import Image
    from playwright.async_api import async_playwright
    from benchmarks.provenance import provenance
    from scripts.diagnose_browser_preview import purple_cursor_bbox
    output.mkdir(parents=True,exist_ok=False)
    before=provenance(ROOT)
    listener=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    listener.bind(('127.0.0.1',0));listener.listen(128)
    port=listener.getsockname()[1]
    assert port!=8765
    base=f'http://127.0.0.1:{port}'
    child_env=os.environ.copy();child_env['COMPUTERUSE_PORT']=str(port)
    process=None;collector=None;browser=None;run_id=None
    report={};raw_frames=[];http_meta={};browser_errors=[];stream_responses=[]
    started=time.monotonic()
    with (output/'server.log').open('w') as log:
        try:
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--serve','--port',str(port),'--fd',str(listener.fileno())],
                cwd=ROOT,env=child_env,pass_fds=(listener.fileno(),),stdout=log,stderr=subprocess.STDOUT)
            listener.close()
            async with httpx.AsyncClient(base_url=base,headers={'x-computeruse':'1'},timeout=10,trust_env=False) as client:
                async with asyncio.timeout(15):
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('Owned uvicorn exited before readiness; inspect server.log')
                        try:
                            response=await client.get('/api/config')
                            if response.status_code==200:break
                        except httpx.TransportError:pass
                        await asyncio.sleep(.1)
                response=await client.post('/api/config',json={'provider':'demo','model':'local-demo','vision':False,'perception':'auto'})
                response.raise_for_status()
                assert response.json()['provider']=='demo'
                response=await client.post('/api/sessions',json={'task':'執行固定本機操作自我檢查','target':'browser','browser_visible':False,'approval_mode':'always','max_steps':8})
                response.raise_for_status();run_id=response.json()['id']
                async with asyncio.timeout(15):
                    while True:
                        session=(await client.get(f'/api/sessions/{run_id}')).json()
                        if session['status']=='awaiting_approval':break
                        if session['status'] in ('failed','stopped'):raise RuntimeError(session)
                        await asyncio.sleep(.06)
                assert session['pending_action']['type']=='type'
                view=f'/api/sessions/{run_id}/view'
                async def collect_http():
                    async with client.stream('GET',view,timeout=30) as response:
                        http_meta.update(status=response.status_code,content_type=response.headers.get('content-type'),cache_control=response.headers.get('cache-control'))
                        response.raise_for_status()
                        buffer=b''
                        async for chunk in response.aiter_bytes():
                            parts,buffer=parse_parts(buffer+chunk)
                            for raw,mime in parts:
                                with Image.open(io.BytesIO(raw)) as image:
                                    assert image.size==(1280,800)
                                    fmt=image.format
                                raw_frames.append({'raw':raw,'mime':mime,'decoded_format':fmt,'sha256':hashlib.sha256(raw).hexdigest(),'elapsed':round(time.monotonic()-started,4)})
                collector=asyncio.create_task(collect_http())
                async with async_playwright() as playwright:
                    browser=await playwright.chromium.launch(headless=True)
                    monitor=await browser.new_page(viewport={'width':1280,'height':860})
                    monitor.on('pageerror',lambda error:browser_errors.append(str(error)))
                    monitor.on('console',lambda message:browser_errors.append('console: '+message.text) if message.type=='error' else None)
                    async def record_response(response):
                        if response.url==base+view:
                            stream_responses.append({'status':response.status,'content_type':(await response.all_headers()).get('content-type')})
                    monitor.on('response',record_response)
                    response=await monitor.goto(base+f'/__proof/monitor/{run_id}',wait_until='domcontentloaded')
                    assert response.status==200
                    await monitor.wait_for_function('live.naturalWidth===1280 && decodedFrames.length>=1',timeout=12000)
                    approvals=[]
                    for expected in ('type','click'):
                        async with asyncio.timeout(12):
                            while True:
                                session=(await client.get(f'/api/sessions/{run_id}')).json()
                                if session['status']=='awaiting_approval' and session['pending_action']['type']==expected:break
                                if session['status'] in ('failed','stopped','completed'):raise RuntimeError(session)
                                await asyncio.sleep(.06)
                        approvals.append({'action':expected,'monitor_natural_width':await monitor.evaluate('live.naturalWidth')})
                        approved=await client.post(f'/api/sessions/{run_id}/approve',json={'approved':True})
                        approved.raise_for_status()
                    async with asyncio.timeout(20):
                        while True:
                            session=(await client.get(f'/api/sessions/{run_id}')).json()
                            if session['status'] not in ('starting','running','awaiting_approval','paused','awaiting_input'):break
                            await asyncio.sleep(.08)
                    async with asyncio.timeout(5):
                        while True:
                            oracle_response=(await client.get(f'/__proof/oracle/{run_id}')).json()
                            if oracle_response['task_done']:break
                            await asyncio.sleep(.05)
                    await asyncio.wait_for(collector,timeout=5)
                    await asyncio.sleep(.25)
                    decoded=await monitor.evaluate('decodedFrames')
                    img_state=await monitor.evaluate('({naturalWidth:live.naturalWidth,naturalHeight:live.naturalHeight,errors:monitorErrors})')
                    await monitor.screenshot(path=output/'monitor-final.png')
                    await browser.close();browser=None
                for frame in raw_frames:
                    frame['cursor_pixels']=await asyncio.to_thread(purple_cursor_bbox,frame['raw']) if frame['mime']=='image/jpeg' else None
                jpeg_frames=[f for f in raw_frames if f['mime']=='image/jpeg']
                raw_positions={tuple(f['cursor_pixels'][:2]) for f in jpeg_frames if f['cursor_pixels']}
                decoded_positions={tuple(f['cursorPixels'][:2]) for f in decoded if f['cursorPixels']}
                for label,index in [('start',0),('middle',len(decoded)//2),('end',len(decoded)-1)]:
                    raw=base64.b64decode(decoded[index]['jpeg'].split(',',1)[1]);(output/f'monitor-{label}.jpg').write_bytes(raw)
                for index,frame in enumerate(jpeg_frames):
                    if index in {0,len(jpeg_frames)//2,len(jpeg_frames)-1}:
                        name=f'http-frame-{index:02d}.jpg';(output/name).write_bytes(frame['raw']);frame['saved_as']=name
                oracle=oracle_response.get('oracle') or {}
                checks={'http_200':http_meta.get('status')==200,
                    'http_multipart_jpeg_type':str(http_meta.get('content_type','')).startswith('multipart/x-mixed-replace; boundary=computeruse-frame'),
                    'browser_img_http_200':bool(stream_responses) and all(r['status']==200 for r in stream_responses),
                    'img_decoded_dimensions':img_state['naturalWidth']==1280 and img_state['naturalHeight']==800,
                    'multiple_http_jpeg_frames':len(jpeg_frames)>=5 and len({f['sha256'] for f in jpeg_frames})>=4,
                    'http_intermediate_cursor_pixels':len(raw_positions)>=3,
                    'browser_decoded_intermediate_cursor_pixels':len(decoded_positions)>=3,
                    'monitor_connected_before_approvals':all(a['monitor_natural_width']==1280 for a in approvals),
                    'demo_task_completed':session['status']=='completed',
                    'independent_actual_dom_oracle':oracle.get('name')=='ComputerUSE' and oracle.get('result')=='測試任務已完成：歡迎，ComputerUSE。' and oracle.get('result_hidden') is False,
                    'no_browser_errors':not browser_errors and not img_state['errors'],
                    'zero_model_calls':oracle_response['model_calls']==0,
                    'headless_agent':session['browser_visible'] is False,
                    'runtime_sources_unchanged':before['source_sha256']==provenance(ROOT)['source_sha256']}
                report={'planner_mode':'scripted_driver_smoke','provider':'demo','model':None,'model_calls':oracle_response['model_calls'],
                    'passed':all(checks.values()),'checks':checks,'http':http_meta,'img_http_responses':stream_responses,
                    'img_state':img_state,'http_frame_count':len(raw_frames),'http_jpeg_count':len(jpeg_frames),
                    'http_distinct_cursor_pixels':sorted(raw_positions),'img_decoded_distinct_cursor_pixels':sorted(decoded_positions),
                    'http_frames':[{k:v for k,v in f.items() if k!='raw'} for f in raw_frames],
                    'img_decoded_frames':[{k:v for k,v in f.items() if k!='jpeg'} for f in decoded],
                    'task_status':session['status'],'session':session,'fixture_oracle':oracle,'oracle_hook':'test-only BrowserDriver.close subclass reads actual demo DOM; action execution and production source unchanged',
                    'browser_errors':browser_errors,'approvals':approvals,'owned_uvicorn_pid':process.pid,'owned_loopback_port':port,
                    'user_port_8765_used':False,'headless_monitor':True,'production_frontend_ui_validated':False,
                    'elapsed_seconds':round(time.monotonic()-started,3),'provenance':before,
                    'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        finally:
            listener.close()
            if collector is not None and not collector.done():
                collector.cancel();await asyncio.gather(collector,return_exceptions=True)
            if browser is not None:await browser.close()
            if process is not None and process.poll() is None:
                process.terminate()
                try:await asyncio.to_thread(process.wait,timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill();await asyncio.to_thread(process.wait,timeout=5)
            if report:
                report['own_server_exited']=process.poll() is not None
                report['own_server_exit_code']=process.returncode
                (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--port',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--fd',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--output',type=Path,default=ROOT/'artifacts/browser-http-preview-proof')
    args=parser.parse_args()
    if args.serve:
        assert args.port and args.fd is not None
        serve(args.port,args.fd);return 0
    report=asyncio.run(diagnose(args.output.resolve()))
    print(json.dumps({k:report[k] for k in ('passed','http','http_jpeg_count','task_status','checks','own_server_exited')},indent=2))
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
