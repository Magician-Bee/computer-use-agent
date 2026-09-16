import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError, downloadArtifact } from './api';
import Icon, { type IconName } from './Icon';
import Settings from './Settings';
import DesktopTarget from './DesktopTarget';
import { isActive, providerNames, statusText, type Action, type DesktopSelection, type Health, type ModelConfig, type Session } from './types';

const START_URL = 'https://www.google.com/';
const demoConfig: ModelConfig = { provider: 'demo', model: 'local-demo', base_url: '', vision: false, perception: 'auto', max_tokens: 2048, ocr_engine: 'auto', ocr_model: 'glm-ocr:latest', ocr_base_url: 'http://127.0.0.1:11434' };
const actionNames: Record<string, string> = { navigate: '開啟網頁', click: '點擊', double_click: '雙擊', type: '輸入文字', key: '按下按鍵', scroll: '捲動畫面', wait: '等待畫面', done: '完成任務', move: '移動游標', drag: '拖曳', open_app: '開啟應用程式', back: '回到上一頁', forward: '前往下一頁', ask_user: '詢問你', upload_file: '上傳已授權的檔案', select_option: '選擇選項', new_tab: '開啟新分頁', switch_tab: '切換分頁', close_tab: '關閉分頁' };
const templates: { icon: IconName; title: string; description: string; task: string; target: 'browser' | 'desktop' }[] = [
 { icon: 'browser', title: '瀏覽與查找', description: '打開網站，找到需要的資訊', task: '開啟 https://www.wikipedia.org，搜尋「人工智慧」，閱讀頁面並整理三個重點。', target: 'browser' },
 { icon: 'desktop', title: '跨應用程式工作', description: '用滑鼠與鍵盤完成桌面任務', task: '開啟計算機，計算 128 × 36，並告訴我畫面上的結果。', target: 'desktop' },
 { icon: 'eye', title: '理解目前畫面', description: '用 OCR 讀取畫面中的文字', task: '觀察目前桌面，用文字說明開啟了什麼應用程式，以及畫面上主要的文字與可操作元素。先不要改動任何內容。', target: 'desktop' },
];
const dateLabel = (value: string) => new Date(value).toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit' });
function actionDetails(action: Action) {
 const chunks: string[] = [];
 if (action.url) chunks.push(action.url);
 if (action.app) chunks.push(action.app);
 if (action.target) chunks.push(`元素 ${action.target}`);
 if (action.x !== undefined && action.y !== undefined) chunks.push(`(${Math.round(action.x)}, ${Math.round(action.y)})`);
 if (action.end_x !== undefined) chunks.push(`→ (${action.end_x}, ${action.end_y})`);
 if (action.button && action.button !== 'left') chunks.push(`${action.button} click`);
 if (action.key) chunks.push(action.key);
 if (action.text) chunks.push(action.text);
 if (action.direction) chunks.push(`${action.direction} ${action.amount || ''}`);
 if (action.seconds) chunks.push(`${action.seconds} 秒`);
 return chunks.join(' · ');
}

export default function App() {
 const [health, setHealth] = useState<Health | null>(null);
 const [config, setConfig] = useState<ModelConfig>(demoConfig);
 const [sessions, setSessions] = useState<Session[]>([]);
 const [selectedId, setSelectedId] = useState<string | null>(null);
 const [settings, setSettings] = useState(false);
 const [loading, setLoading] = useState(true);
 const [busy, setBusy] = useState<string | null>(null);
 const [transferBusy, setTransferBusy] = useState(false);
 const [error, setError] = useState<string | null>(null);
 const [task, setTask] = useState('');
 const [target, setTarget] = useState<'browser' | 'desktop'>('browser');
 const [desktopSelection, setDesktopSelection] = useState<DesktopSelection | null>(null);
 const [desktopAvailable, setDesktopAvailable] = useState(false);
 const [approval, setApproval] = useState<'always' | 'auto'>('always');
 const [maxSteps, setMaxSteps] = useState(30);
 const [advanced, setAdvanced] = useState(false);
 const [browserVisible, setBrowserVisible] = useState(false);
 const [allowedFiles, setAllowedFiles] = useState('');
 const [previewMode, setPreviewMode] = useState<'screen' | 'elements'>('screen');
 const [expanded, setExpanded] = useState(false);
 const [menu, setMenu] = useState(false);
 const [note, setNote] = useState('');
 const [noteResult, setNoteResult] = useState('');
 const [screenshotFailed, setScreenshotFailed] = useState(false);
 const [approvalSent, setApprovalSent] = useState<string | null>(null);
 const composer = useRef<HTMLTextAreaElement>(null);
 const logBottom = useRef<HTMLDivElement>(null);
 const session = sessions.find(item => item.id === selectedId);
 const activeSession = sessions.find(isActive);
 const activeId = activeSession?.id;
 const observation = session?.observation;
 const pendingKey = session?.pending_action ? `${session.id}:${session.step}:${JSON.stringify(session.pending_action)}` : null;
 const doneMessage = session?.status === 'completed' ? [...session.events].reverse().find(event => event.kind === 'done')?.message : null;
 const perception = health?.capabilities.perception || health?.capabilities;
 const desktopReady = health?.capabilities.desktop === true && desktopAvailable && !!desktopSelection;
 const liveBrowserPreview = session?.target === 'browser' && isActive(session);
 const screenshotUrl = session?.screenshot_available ? `/api/sessions/${encodeURIComponent(session.id)}/${liveBrowserPreview ? 'view' : `screenshot?v=${encodeURIComponent(session.updated_at)}`}` : null;

 const updateSession = useCallback((next: Session) => setSessions(current => {
   const found = current.some(item => item.id === next.id);
   return found ? current.map(item => item.id === next.id && Date.parse(next.updated_at) >= Date.parse(item.updated_at) ? next : item) : [next, ...current];
 }), []);
 const initialize = useCallback(async () => {
   setLoading(true); setError(null);
   try {
     const [nextHealth, nextConfig, nextSessions] = await Promise.all([api<Health>('/health'), api<ModelConfig>('/config'), api<Session[]>('/sessions')]);
     setHealth(nextHealth); setConfig({ ...demoConfig, ...nextConfig }); setSessions(nextSessions);
     const active = nextSessions.find(isActive);
     if (active) setSelectedId(active.id);
   } catch (problem) { setError(problem instanceof Error ? problem.message : '無法連線到本機服務。'); setHealth(null); }
   finally { setLoading(false); }
 }, []);
 useEffect(() => { void initialize(); }, [initialize]);
 useEffect(() => {
   if (!activeId) return;
   let disposed = false;
   let timeout: number;
   const controller = new AbortController();
   const poll = async () => {
     try {
       const latest = await api<Session>(`/sessions/${encodeURIComponent(activeId)}`, undefined, controller.signal);
       if (!disposed) updateSession(latest);
     } catch (problem) {
       if (!disposed && problem instanceof ApiError && problem.status === 404) {
         setSelectedId(current => current === activeId ? null : current);
         setSessions(current => current.filter(item => item.id !== activeId));
         await initialize();
         setError('任務紀錄已不存在，本機服務可能已重新啟動。已重新載入模型與任務狀態。');
         return;
       }
       if (!disposed) setError(problem instanceof Error ? problem.message : '更新任務狀態失敗。');
     }
     if (!disposed) timeout = window.setTimeout(() => void poll(), 900);
   };
   timeout = window.setTimeout(() => void poll(), 900);
   return () => { disposed = true; controller.abort(); window.clearTimeout(timeout); };
 }, [activeId, updateSession, initialize]);
 useEffect(() => { setScreenshotFailed(false); }, [screenshotUrl, session?.updated_at]);
 useEffect(() => {
   if (!screenshotFailed || !isActive(session)) return;
   const retry = window.setTimeout(() => setScreenshotFailed(false), 3000);
   return () => window.clearTimeout(retry);
 }, [screenshotFailed, session?.id, session?.status]);
 useEffect(() => { setApprovalSent(null); }, [pendingKey]);
 useEffect(() => { if (session && isActive(session)) logBottom.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [session?.events.length]);
 useEffect(() => { setNote(''); setNoteResult(''); }, [selectedId]);
 useEffect(() => {
   if (!expanded) return;
   const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setExpanded(false); };
   window.addEventListener('keydown', close);
   return () => window.removeEventListener('keydown', close);
 }, [expanded]);
 const closeSettings = useCallback(() => setSettings(false), []);
 const newTask = () => { setSelectedId(null); setTask(''); setDesktopSelection(null); setApproval('always'); setAllowedFiles(''); setMaxSteps(30); setBrowserVisible(false); setAdvanced(false); setError(null); setMenu(false); window.setTimeout(() => composer.current?.focus(), 50); };
 const selectSession = async (id: string) => {
   setSelectedId(id); setMenu(false); setError(null); setScreenshotFailed(false);
   try { updateSession(await api<Session>(`/sessions/${encodeURIComponent(id)}`)); }
   catch (problem) { setError(problem instanceof Error ? problem.message : '無法讀取任務。'); }
 };
 const run = async (demo = false) => {
   if (loading || activeSession || busy) return;
   if ((!demo && config.provider === 'demo') || (demo && config.provider !== 'demo')) { setSettings(true); return; }
   if (!demo && !task.trim()) { composer.current?.focus(); return; }
   if (!demo && target === 'desktop' && !desktopReady) { setError('請先確認背景輸入可用，並選擇本次目標視窗。'); return; }
   setBusy('run'); setError(null);
   if (!demo && target === 'browser' && allowedFiles.split('\n').map(path => path.trim()).filter(Boolean).length > 20) { setError('每次任務最多允許 20 個上傳檔案。'); setBusy(null); return; }
   try {
     if (demo) setConfig(await api<ModelConfig>('/config', demoConfig));
     const next = await api<Session>('/sessions', { task: demo ? '在本機示範頁面輸入 ComputerUSE，點擊「完成測試」，並確認「測試任務已完成」。' : task.trim(), target: demo ? 'browser' : target, approval_mode: demo ? 'always' : approval, max_steps: demo ? 12 : maxSteps, start_url: demo || target === 'desktop' ? 'about:blank' : START_URL, browser_visible: (demo || target === 'browser') && browserVisible, allowed_uploads: demo || target === 'desktop' ? [] : allowedFiles.split('\n').map(path => path.trim()).filter(Boolean), ...(!demo && target === 'desktop' && desktopSelection ? { desktop_pid: desktopSelection.pid, desktop_window_id: desktopSelection.window_id } : {}) });
     updateSession(next); setSelectedId(next.id); setPreviewMode('screen');
   } catch (problem) { setError(problem instanceof Error ? problem.message : '無法開始任務。'); }
   finally { setBusy(null); }
 };
 const control = async (operation: 'stop' | 'approve' | 'pause' | 'resume' | 'input', payload: object = {}) => {
   if (!session || (busy && operation !== 'stop')) return;
   setBusy(operation); setError(null);
   try {
     updateSession(await api<Session>(`/sessions/${encodeURIComponent(session.id)}/${operation}`, payload));
     if (operation === 'approve') setApprovalSent(pendingKey);
     if (operation === 'input') { setNote(''); setNoteResult('已送出，代理會在下一次決策時讀取。'); }
   } catch (problem) { setError(problem instanceof Error ? problem.message : '操作失敗。'); }
   finally { setBusy(current => current === operation ? null : current); }
 };
 const exportSession = async () => {
   if (!session || transferBusy) return;
   setTransferBusy(true);
   try {
     await downloadArtifact(`/sessions/${encodeURIComponent(session.id)}/export`, `computeruse-${session.id}.json`);
   } catch (problem) { setError(problem instanceof Error ? problem.message : '匯出失敗。'); }
   finally { setTransferBusy(false); }
 };
 const downloadFile = async (file: { id: string; name: string }) => {
   if (!session || transferBusy) return;
   setTransferBusy(true); setError(null);
   try { await downloadArtifact(`/sessions/${encodeURIComponent(session.id)}/files/${encodeURIComponent(file.id)}`, file.name); }
   catch (problem) { setError(problem instanceof Error ? problem.message : '無法下載任務檔案。'); }
   finally { setTransferBusy(false); }
 };
 return <div className="app-shell">
   {menu && <div className="sidebar-scrim" onClick={() => setMenu(false)} />}
   <aside className={`sidebar ${menu ? 'open' : ''}`}>
     <a className="brand" href="#" onClick={event => { event.preventDefault(); newTask(); }}><span className="brand-mark"><Icon name="cursor" size={23}/></span><span>Computer<span className="brand-use">USE</span><span className="brand-sub">你的模型的行動工作區</span></span></a>
     <button className="new-task" onClick={newTask}><Icon name="plus" size={19}/><span>新任務</span><span className="keyboard-hint">↗</span></button>
     <div className="sidebar-section"><span>工作區</span><span className="tiny-label">LOCAL</span></div>
     <button className={`sidebar-nav ${!selectedId ? 'active' : ''}`} onClick={newTask}><Icon name="layers" size={18}/><span>任務工作台</span></button>
     <button className="sidebar-nav" onClick={() => setSettings(true)}><Icon name="settings" size={18}/><span>模型與感知</span><Icon name="arrow" size={14}/></button>
     <div className="sidebar-section history-heading"><span>最近的任務</span><span>{sessions.length.toString().padStart(2, '0')}</span></div>
     <div className="history-list">{sessions.length === 0 ? <div className="history-empty"><Icon name="clock" size={24}/><p>每一次行動，<br/>都會在這裡留下紀錄。</p></div> : sessions.map(item => <button key={item.id} className={`history-item ${selectedId === item.id ? 'selected' : ''}`} onClick={() => void selectSession(item.id)}><span className={`history-status ${item.status}`}><Icon name={item.status === 'completed' ? 'check' : item.status === 'failed' ? 'alert' : isActive(item) ? 'cursor' : 'clock'} size={13}/></span><span><strong>{item.provider === 'demo' ? '本機互動示範' : item.task}</strong><small>{dateLabel(item.created_at)} · {statusText[item.status]}</small></span></button>)}</div>
     <div className="sidebar-bottom"><div className="local-status"><span className={`status-dot ${health?.ok ? 'green' : 'grey'}`}/><span>{loading ? '連接本機服務…' : health?.ok ? '本機服務運作中' : '本機服務未連線'}</span><button className="icon-button small" title="重新檢查服務" aria-label="重新檢查服務" onClick={() => void initialize()} disabled={loading}><Icon name="refresh" size={14} className={loading ? 'spin' : ''}/></button></div><button className="model-profile" onClick={() => setSettings(true)}><span className="model-avatar"><Icon name={config.provider === 'demo' ? 'play' : 'spark'} size={19}/></span><span><strong>{config.provider === 'demo' ? '連接你的模型' : config.model}</strong><small>{config.provider === 'demo' ? 'OpenAI · Ollama · 自訂 API' : providerNames[config.provider]}</small></span><Icon name="settings" size={17}/></button><div className="sidebar-footnote">在你的電腦，照你的方式。<span>{health?.version ? `v${health.version}` : 'ComputerUSE'}</span></div></div>
   </aside>
   <div className="workspace">
     <header className="topbar"><div className="breadcrumbs"><button className="icon-button mobile-menu" onClick={() => setMenu(true)} aria-label="開啟導覽"><Icon name="menu"/></button><Icon name="layers" size={17}/><span>工作區</span><span className="slash">/</span><strong>{session ? '任務詳情' : '新任務'}</strong></div><div className="header-right"><span className="local-pill"><span className="status-dot green"/>本機工作區</span><button className="header-model" onClick={() => setSettings(true)}><Icon name="spark" size={15}/><span>{config.provider === 'demo' ? '尚未連接模型' : config.model}</span><Icon name="chevron" size={14}/></button></div></header>
     <main>
       {error && <div className="global-error" role="alert"><Icon name="alert" size={19}/><div><strong>{health ? '操作未完成' : '無法連線到本機服務'}</strong><p>{error}{!health && ' 請確認後端已啟動於 127.0.0.1:8765。'}</p></div><button className="icon-button" onClick={() => setError(null)} aria-label="關閉錯誤訊息"><Icon name="close" size={17}/></button></div>}
       <div className="page-heading"><div><div className="eyebrow"><span className="tiny-star">✳</span>{session ? 'FROM INTENT TO ACTION' : 'A LITTLE LESS CLICKING. A LITTLE MORE DOING.'}</div><h1>{session ? session.provider === 'demo' ? '看看代理如何動手。' : '讓想法，正在發生。' : <>任何模型，<span>都能動手。</span></>}</h1><p>{session ? '觀察畫面、決定動作、執行並再次確認。每一步都看得見。' : '說出你想完成的事，讓 AI 看懂畫面，操作瀏覽器與電腦。'}</p></div><div className="heading-caption"><span>THINK → SEE → ACT</span><Icon name="cursor" size={31}/></div></div>
       {!selectedId && activeSession && <button className="active-banner" onClick={() => void selectSession(activeSession.id)}><span className="pulse-dot"/>目前有任務{statusText[activeSession.status]}，完成或停止後即可開始新任務。<Icon name="arrow" size={17}/></button>}
       <div className="work-grid">
         <section className="task-column">
           {session ? <>
             <div className="session-card"><div className="session-card-top"><span className={`status-badge ${session.status}`}><span className="status-dot"/>{statusText[session.status]}</span><div className="session-actions"><button className="icon-button" onClick={() => void exportSession()} disabled={transferBusy} title="匯出任務紀錄" aria-label="匯出任務紀錄"><Icon name="download" size={17}/></button>{isActive(session) && <><button className="button compact secondary" disabled={!!busy} onClick={() => void control(session.status === 'paused' ? 'resume' : 'pause')}><Icon name={session.status === 'paused' ? 'play' : 'clock'} size={14}/>{session.status === 'paused' ? '繼續' : '暫停'}</button><button className="button compact stop-button" disabled={busy === 'stop'} onClick={() => void control('stop')}><Icon name="stop" size={14}/>{busy === 'stop' ? '停止中' : '停止'}</button></>}</div></div><h2>{session.task}</h2><div className="session-meta"><span><Icon name={session.target === 'browser' ? 'browser' : 'desktop'} size={15}/>{session.target === 'browser' ? '隔離瀏覽器' : '本機桌面'}</span><span>{session.model}</span>{session.target === 'desktop' && session.desktop_window_id != null && <span>視窗 #{session.desktop_window_id}{session.desktop_pid != null ? ` · PID ${session.desktop_pid}` : ''}</span>}{session.target === 'browser' && session.browser_visible && <span>可手動接手</span>}<span>{session.approval_mode === 'always' ? '每步確認' : '自動執行'}</span></div><div className="step-progress"><span style={{ width: `${Math.min(session.step / session.max_steps * 100, 100)}%` }}/></div><div className="step-count"><span>{session.status === 'completed' ? '任務已完成' : '執行進度'}</span><span>{session.step} / {session.max_steps} 步</span></div></div>
             {session.error && <div className="session-error" role="alert"><Icon name="alert" size={17}/><span>{session.error}</span></div>}{doneMessage && <div className="completion-card"><Icon name="check" size={18}/><div><strong>任務結果</strong><p>{doneMessage}</p></div></div>}
             {session.provider === 'demo' && <div className="demo-session-note"><Icon name="play" size={16}/><span>固定步驟的本機示範 · 未使用 AI 模型</span></div>}
             {session.status === 'awaiting_approval' && session.pending_action && <div className="approval-card"><div className="approval-label"><Icon name="shield" size={18}/>下一步，需要你確認</div><h3>{actionNames[session.pending_action.type] || session.pending_action.type}</h3><p>{session.pending_action.reason}</p><pre>{actionDetails(session.pending_action)}</pre><div className="approval-buttons"><button className="button secondary" disabled={!!busy || approvalSent === pendingKey} onClick={() => void control('approve', { approved: false })}>拒絕並停止</button><button className="button primary" disabled={!!busy || approvalSent === pendingKey} onClick={() => void control('approve', { approved: true })}>{approvalSent === pendingKey ? '確認已送出' : '允許這一步'}<Icon name="arrow" size={16}/></button></div></div>}
             {session.status === 'paused' && <div className="neutral-note"><Icon name="clock"/><p>任務已暫停。你可以查看畫面、補充指示，準備好後點「繼續」。</p></div>}
             <div className="activity-card"><div className="card-header"><h2><Icon name="layers" size={17}/>操作紀錄</h2><span className="muted-label">{session.events.length} EVENTS</span></div><div className="timeline">{session.events.length === 0 ? <div className="timeline-empty"><span className="spinner"/>正在準備操作環境…</div> : session.events.map((event, index) => <div className={`timeline-event event-${event.kind}`} key={event.id || `${event.at}-${index}`}><span className="event-symbol"><Icon name={event.kind === 'action' ? 'cursor' : event.kind === 'error' ? 'alert' : event.kind === 'done' || event.kind === 'result' ? 'check' : event.kind === 'observation' ? 'eye' : 'spark'} size={14}/></span><div className="event-body"><div className="event-heading"><strong>{event.action ? actionNames[event.action.type] || event.action.type : { info: '代理狀態', observation: '觀察畫面', result: '操作結果', error: '遇到問題', done: '任務結果', action: '執行動作' }[event.kind]}</strong><time>{dateLabel(event.at)}</time></div><p>{event.message}</p>{event.action && actionDetails(event.action) && <div className="event-details">{actionDetails(event.action)}</div>}</div></div>)}<div ref={logBottom}/></div></div>
             {session.downloads && session.downloads.length > 0 && <div className="download-card"><h2><Icon name="download" size={16}/>任務產生的檔案</h2>{session.downloads.map(file => <button key={file.id} disabled={transferBusy} onClick={() => void downloadFile(file)} className="download-item"><Icon name="download" size={16}/><span><strong>{file.name}</strong><small>{file.size < 1024 * 1024 ? `${Math.max(1, Math.round(file.size / 1024))} KB` : `${(file.size / (1024 * 1024)).toFixed(1)} MB`}</small></span><Icon name="arrow" size={15}/></button>)}</div>}
             {isActive(session) && <div className={`intervention-card ${session.status === 'awaiting_input' ? 'needs-input' : ''}`}><label htmlFor="agent-note"><Icon name="plus" size={17}/>{session.status === 'awaiting_input' ? '代理需要你的補充' : '隨時補充指示'}</label>{session.pending_input && <p className="pending-question">{session.pending_input}</p>}{session.status === 'awaiting_input' && session.target === 'browser' && session.browser_visible && <p className="handoff-note"><Icon name="browser" size={15}/><span>遇到登入或驗證時，可直接在此視窗手動完成，再於任務中回覆繼續。</span></p>}<div className="note-compose"><textarea id="agent-note" maxLength={8000} rows={2} placeholder="補充細節，或調整接下來的方向…" value={note} onChange={e => { setNote(e.target.value); setNoteResult(''); }}/><button className="icon-button send-note" aria-label="送出補充指示" disabled={!note.trim() || !!busy} onClick={() => void control('input', { text: note.trim() })}><Icon name="arrow" size={18}/></button></div>{noteResult && <p className="field-hint" role="status">{noteResult}</p>}</div>}
             {!isActive(session) && <button className="button secondary rerun-button" onClick={() => { newTask(); setTask(session.task); setTarget(session.target); }}><Icon name="refresh" size={17}/>以這個任務再試一次</button>}
           </> : <>
             <div className="composer-card"><div className="card-header"><h2><Icon name="spark" size={18}/>今天，要完成什麼？</h2><span className="muted-label">NEW TASK</span></div><textarea className="task-input" maxLength={8000} ref={composer} value={task} onChange={event => setTask(event.target.value)} placeholder="例如：打開網站，找到需要的資料，並整理結果…" aria-label="任務描述" onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); void run(); } }}/><div className="composer-bottom"><div className="target-toggle" role="group" aria-label="操作環境"><button className={target === 'browser' ? 'selected' : ''} onClick={() => setTarget('browser')}><Icon name="browser" size={16}/>瀏覽器</button><button className={target === 'desktop' ? 'selected' : ''} onClick={() => setTarget('desktop')}><Icon name="desktop" size={16}/>桌面</button></div><button className="text-button" onClick={() => setAdvanced(!advanced)} aria-expanded={advanced}><Icon name="settings" size={15}/>執行設定<Icon name="chevron" size={12} className={advanced ? 'rotate' : ''}/></button></div>
               {target === 'desktop' && <DesktopTarget capabilities={health?.capabilities} selection={desktopSelection} onChange={setDesktopSelection} onAvailability={setDesktopAvailable} onHealth={setHealth}/> }
               {advanced && <div className="run-settings">{target === 'browser' && <><label htmlFor="start-url">起始網址（固定）</label><input id="start-url" value={START_URL} readOnly spellCheck={false}/><p className="field-hint">從 Google 開始，再依任務前往需要的網站。</p><label className="browser-visibility" htmlFor="browser-visible"><input id="browser-visible" type="checkbox" checked={browserVisible} onChange={event => setBrowserVisible(event.target.checked)}/><span><strong>顯示獨立瀏覽器視窗</strong><small>預設在工作台即時預覽代理專用瀏覽器。僅在需要手動接手時勾選；實體視窗可能取得焦點。</small></span></label></>}<div className="run-setting-row"><label htmlFor="max-steps">最多執行步數<span>每一步後重新觀察畫面</span></label><input id="max-steps" type="number" min={1} max={200} value={maxSteps} onChange={event => setMaxSteps(Math.max(1, Math.min(200, Number(event.target.value))))}/></div>{target === 'browser' && <><label className="upload-label" htmlFor="allowed-files">可上傳的檔案<span>選填 · 每行一個絕對路徑，最多 20 個</span></label><textarea id="allowed-files" className="allowed-files" rows={2} value={allowedFiles} onChange={event => setAllowedFiles(event.target.value)} placeholder="/Users/your-name/Documents/report.pdf" spellCheck={false}/><p className="field-hint">代理只能上傳本次列出的檔案。下載會儲存於獨立的任務資料夾，完成後可在任務中取得。</p></>}<div className="toggle-field"><div><strong>自動執行</strong><p>直接操作滑鼠與鍵盤，無須每步確認。你可隨時暫停或停止。</p></div><button className={`switch ${approval === 'auto' ? 'on' : ''}`} role="switch" aria-checked={approval === 'auto'} aria-label="自動執行" onClick={() => setApproval(approval === 'auto' ? 'always' : 'auto')}><span/></button></div></div>}
               <div className="run-bar"><div><Icon name={approval === 'always' ? 'shield' : 'cursor'} size={16}/><span>{approval === 'always' ? '每一步，由你確認' : '已開啟自動執行'}</span></div><button className="button primary run-button" onClick={() => void run()} disabled={loading || !!busy || !!activeSession || !health?.ok || (config.provider !== 'demo' && (!task.trim() || (target === 'desktop' && !desktopReady)))}>{busy === 'run' ? '準備中…' : config.provider === 'demo' ? '連接模型' : '開始任務'}<Icon name="arrow" size={18}/></button></div>
             </div>
             <div className="context-note"><Icon name={target === 'desktop' ? 'desktop' : 'shield'} size={16}/><p>{target === 'browser' ? '預設使用背景的代理專用瀏覽器，在工作台即時監看，不佔用你的滑鼠與鍵盤。每次任務都從乾淨的工作階段開始。' : health?.capabilities.desktop ? '桌面任務只送往你選定的背景視窗。Agent 的輸入不得干擾你的滑鼠、鍵盤或目前焦點。' : health?.capabilities.desktop_hint || '背景獨立輸入尚未就緒，桌面任務目前停用。'}</p></div>
             <div className="template-heading"><h2>從一個想法開始</h2><span>填入後即可編輯</span></div><div className="template-grid">{templates.map(template => <button className="template-card" aria-label={`${template.title}：${template.description}`} key={template.title} onClick={() => { setTask(template.task); setTarget(template.target); composer.current?.focus(); }}><span className="template-icon"><Icon name={template.icon} size={20}/></span><strong>{template.title}</strong><p>{template.description}</p><Icon name="arrow" size={16} className="template-arrow"/></button>)}</div>
             <div className="demo-card"><span className="demo-icon"><Icon name="play" size={18}/></span><div><strong>先看一次，代理如何操作</strong><p>不需 API Key 的本機互動示範</p></div><button className="text-button" disabled={loading || !!busy || !!activeSession || !health?.ok || health?.capabilities.browser === false} onClick={() => void run(true)}>{config.provider === 'demo' ? '試跑示範' : '切換示範模式'}<Icon name="arrow" size={16}/></button></div>
             <div className="pipeline"><div><span>01</span><Icon name="eye" size={18}/><strong>感知</strong><small>截圖 · DOM · OCR</small></div><Icon name="arrow" size={15}/><div><span>02</span><Icon name="spark" size={18}/><strong>思考</strong><small>你選擇的模型</small></div><Icon name="arrow" size={15}/><div><span>03</span><Icon name="cursor" size={18}/><strong>行動</strong><small>操作 · 驗證 · 重試</small></div></div>
           </>}
         </section>
         <section className="preview-column"><div className="preview-card"><div className="preview-header"><h2><Icon name="eye" size={17}/>即時畫面</h2><div><span className={`preview-status ${liveBrowserPreview && !screenshotFailed && session?.screenshot_available ? 'live' : ''}`}><span className="status-dot"/>{screenshotFailed ? isActive(session) ? '重新連線' : '載入失敗' : session?.screenshot_available ? liveBrowserPreview ? 'LIVE' : isActive(session) ? '觀察截圖' : '最後畫面' : 'STANDBY'}</span><button className="icon-button" disabled={!screenshotUrl || screenshotFailed} onClick={() => setExpanded(true)} aria-label="放大截圖" title="放大截圖"><Icon name="expand" size={16}/></button></div></div><div className="preview-tabs"><button className={previewMode === 'screen' ? 'selected' : ''} onClick={() => setPreviewMode('screen')}><Icon name="browser" size={14}/>畫面</button><button className={previewMode === 'elements' ? 'selected' : ''} onClick={() => setPreviewMode('elements')}><Icon name="code" size={14}/>模型的文字視野{observation && <span>{observation.elements.length}</span>}</button></div><div className="preview-url"><Icon name={session?.target === 'desktop' ? 'desktop' : 'link'} size={12}/><span>{observation?.url || (session?.target === 'desktop' ? observation?.title || '本機桌面' : '尚未開啟瀏覽器')}</span></div>
           {previewMode === 'screen' ? <div className={`screen-area ${screenshotUrl ? 'has-screen' : ''}`}>{screenshotUrl && !screenshotFailed ? <img src={screenshotUrl} alt={liveBrowserPreview ? '代理瀏覽器的即時監看畫面' : `${session?.target === 'desktop' ? '桌面' : '瀏覽器'}的最後觀察截圖`} onError={() => setScreenshotFailed(true)}/> : <div className="screen-empty"><div className="orbit-illustration"><div className="orbit-ring ring-one"/><div className="orbit-ring ring-two"/><span className="orbit-element orbit-eye"><Icon name="eye" size={20}/></span><span className="orbit-element orbit-text">Aa</span><span className="orbit-element orbit-action"><Icon name="cursor" size={18}/></span><div className="empty-monitor"><div className="monitor-bar"><i/><i/><i/></div><div className="monitor-content"><Icon name="cursor" size={33}/></div><div className="monitor-stand"/></div></div><h3>{screenshotFailed ? '截圖暫時無法載入' : session ? '正在等待第一張畫面' : '下一步，在這裡看見。'}</h3><p>{screenshotFailed ? isActive(session) ? '正在重新連線，即時畫面將自動重試。' : '請重新開啟此任務以載入最後畫面。' : session ? '代理建立操作環境後，會在此顯示實際截圖。' : '開始任務後，這裡會顯示真實截圖。\n每一次點擊，每一個進展，都清楚可見。'}</p><span className="standby-pill"><span className="status-dot"/>{session ? '等待觀察' : '準備好接受任務'}</span></div>}</div> : <div className="observation-area">{observation ? <><div className="observation-summary"><span className="eyebrow">OBSERVATION</span><h3>{observation.title || '目前畫面'}</h3><p>{observation.text || '尚未擷取到可讀文字。'}</p></div><div className="element-list">{observation.elements.map(element => <div className="element-item" key={element.id}><code>{element.id}</code><span><strong>{element.text || '無文字標籤'}</strong><small>{element.role}{element.source ? ` · ${element.source}` : ''}</small></span></div>)}</div></> : <div className="text-empty"><Icon name="code" size={30}/><h3>純文字，也能看懂畫面。</h3><p>DOM、OCR 與物件辨識會把畫面轉成文字、元素 ID 與位置，提供給模型決定下一步。</p></div>}</div>}
           <div className="preview-footer"><span><Icon name="clock" size={13}/>{session?.screenshot_available ? liveBrowserPreview ? '代理專用瀏覽器 · 即時監看' : `更新於 ${dateLabel(session.updated_at)}` : '尚無觀察畫面'}</span><span>{observation ? `${observation.width} × ${observation.height}` : '等待任務啟動'}</span></div></div>
           <div className="perception-card"><div className="perception-heading"><span className="perception-icon"><Icon name="layers" size={18}/></span><div><strong>模型不會看圖？也沒關係。</strong><p>感知與推理，各司其職。</p></div><button className="icon-button" onClick={() => setSettings(true)} title="設定感知方式" aria-label="設定感知方式"><Icon name="settings" size={16}/></button></div><div className="perception-path"><span>畫面</span><Icon name="arrow" size={13}/><span>OCR + 元素</span><Icon name="arrow" size={13}/><span>任何文字 LLM</span></div><div className="capability-summary"><span><i className={health?.capabilities.browser ? 'ready' : ''}/>DOM {health?.capabilities.browser ? '就緒' : '未就緒'}</span><span><i className={perception?.ocr ? 'ready' : ''}/>OCR {perception?.ocr ? '就緒' : '未就緒'}</span><span><i className={perception?.yolo ? 'ready' : ''}/>YOLO {perception?.yolo ? '就緒' : '選用／未載入'}</span></div></div>
           <div className="transmission-note"><Icon name="shield" size={15}/><p>{config.provider === 'demo' ? '本機示範不傳送畫面至外部服務。連接模型後，資料會傳至你指定的端點。' : config.vision ? '已啟用影像：截圖、畫面文字與任務會傳至設定的模型端點。' : '文字模式：模型只接收畫面文字、元素位置與任務，不接收截圖。'}</p></div>
         </section>
       </div>
       <footer className="workspace-footer"><span>BUILT FOR MODELS. MADE FOR YOU.</span><span>ComputerUSE <span className="footer-dot">·</span> 觀察，思考，行動。</span></footer>
     </main>
   </div>
   {settings && <Settings config={config} capabilities={health?.capabilities} onClose={closeSettings} onSaved={next => { setConfig({ ...demoConfig, ...next }); setSettings(false); void api<Health>('/health').then(setHealth).catch(() => {}); }}/ >}
   {expanded && screenshotUrl && <div className="screenshot-modal" role="dialog" aria-modal="true" aria-label="完整截圖" onClick={() => setExpanded(false)}><button className="icon-button" onClick={() => setExpanded(false)} aria-label="關閉完整截圖" autoFocus><Icon name="close" size={24}/></button><img src={screenshotUrl} alt={liveBrowserPreview ? '代理瀏覽器的完整即時監看畫面' : '完整觀察截圖'} onClick={event => event.stopPropagation()}/></div>}
 </div>;
}
