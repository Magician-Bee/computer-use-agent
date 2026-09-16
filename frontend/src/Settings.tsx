import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import Icon from './Icon';
import { providerNames, type Capabilities, type ModelConfig, type Provider } from './types';

const defaults: Record<Provider, { base_url: string; model: string }> = {
 openai: { base_url: 'https://api.openai.com/v1', model: '' },
 anthropic: { base_url: 'https://api.anthropic.com', model: '' },
 gemini: { base_url: 'https://generativelanguage.googleapis.com', model: '' },
 ollama: { base_url: 'http://127.0.0.1:11434', model: '' },
 custom: { base_url: '', model: '' },
 demo: { base_url: '', model: 'local-demo' },
};
const providers: Provider[] = ['openai', 'anthropic', 'gemini', 'ollama', 'custom', 'demo'];

export default function Settings({ config, capabilities, onClose, onSaved }: { config: ModelConfig; capabilities?: Capabilities; onClose: () => void; onSaved: (config: ModelConfig) => void }) {
 const savedEndpoint = config.base_url || defaults[config.provider].base_url;
 const initialForm = { ...config, base_url: savedEndpoint, api_key: '' };
 const [form, setForm] = useState<ModelConfig>(initialForm);
 const providerDrafts = useRef<Partial<Record<Provider, ModelConfig>>>({ [config.provider]: initialForm });
 const [busy, setBusy] = useState<'save' | 'test' | 'clear_key' | null>(null);
 const [feedback, setFeedback] = useState<{ ok: boolean; message: string } | null>(null);
 const [models, setModels] = useState<{ name: string; capabilities?: string[] }[]>([]);
 const [modelStatus, setModelStatus] = useState('');
 const panel = useRef<HTMLDivElement>(null);
 const feedbackElement = useRef<HTMLDivElement>(null);
 useEffect(() => { if (feedback) feedbackElement.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }); }, [feedback]);
 const perception = capabilities?.perception || capabilities;
 useEffect(() => {
   if (form.provider !== 'ollama' || !['http://127.0.0.1:11434', 'http://localhost:11434'].includes(form.base_url.replace(/\/$/, ''))) { setModels([]); setModelStatus(''); return; }
   const controller = new AbortController();
   setModelStatus('正在讀取本機已安裝模型…');
   api<{ models: { name: string; capabilities?: string[] }[]; error?: string }>('/models', undefined, controller.signal).then(result => { setModels(result.models || []); setModelStatus(result.models?.length ? '本機已安裝模型 · 點選帶入' : result.error || '本機 Ollama 未回傳已安裝模型，仍可手動輸入模型 ID。'); }).catch(error => { if (!controller.signal.aborted) setModelStatus(error instanceof Error ? error.message : '無法讀取本機模型清單，可手動輸入 ID。'); });
   return () => controller.abort();
 }, [form.provider, form.base_url]);
 const set = <K extends keyof ModelConfig>(key: K, value: ModelConfig[K]) => { setForm(current => ({ ...current, [key]: value })); setFeedback(null); };
 useEffect(() => {
   const before = document.activeElement as HTMLElement | null;
   panel.current?.focus();
   const handler = (event: KeyboardEvent) => {
     if (event.key === 'Escape') onClose();
     if (event.key === 'Tab') {
       const elements = panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]');
       if (!elements?.length) return;
       const first = elements[0], last = elements[elements.length - 1];
       if (event.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { event.preventDefault(); last.focus(); }
       else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
     }
   };
   document.addEventListener('keydown', handler);
   const previous = document.body.style.overflow;
   document.body.style.overflow = 'hidden';
   return () => { document.removeEventListener('keydown', handler); document.body.style.overflow = previous; before?.focus(); };
 }, [onClose]);
 const changeProvider = (provider: Provider) => {
   if (provider === form.provider) return;
   providerDrafts.current[form.provider] = { ...form };
   setForm(providerDrafts.current[provider] || { ...form, provider, ...defaults[provider], api_key: '', has_key: false, vision: provider === 'demo' ? false : form.vision });
   setFeedback(null);
 };
 const clearKey = async () => {
   setBusy('clear_key'); setFeedback(null);
   try { onSaved(await api<ModelConfig>('/config/key', {}, undefined, 'DELETE')); }
   catch (error) { setFeedback({ ok: false, message: error instanceof Error ? error.message : '無法清除金鑰。' }); }
   finally { setBusy(null); }
 };
 const submit = async (mode: 'save' | 'test') => {
   setBusy(mode); setFeedback(null);
   try {
     if (form.provider !== 'demo' && (!form.model.trim() || !form.base_url.trim())) throw new Error('請填入模型 ID 與 API 位址。');
     const payload = { provider: form.provider, model: form.model.trim(), base_url: form.base_url.trim(), api_key: form.api_key || '', vision: form.vision, perception: form.perception, max_tokens: form.max_tokens, ocr_engine: form.ocr_engine || 'auto', ocr_model: form.ocr_model || 'glm-ocr:latest', ocr_base_url: form.ocr_base_url || 'http://127.0.0.1:11434' };
     if (mode === 'test') setFeedback(await api<{ ok: boolean; message: string }>('/config/test', payload));
     else onSaved(await api<ModelConfig>('/config', payload));
   } catch (error) { setFeedback({ ok: false, message: error instanceof Error ? error.message : '無法連線，請稍後再試。' }); }
   finally { setBusy(null); }
 };
 return <div className="modal-scrim" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose(); }}>
   <div className="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabIndex={-1} ref={panel}>
     <div className="settings-heading"><div><div className="eyebrow">YOUR MODEL, YOUR WAY</div><h2 id="settings-title">模型與感知</h2><p>把熟悉的模型，接上行動能力。</p></div><button className="icon-button" onClick={onClose} aria-label="關閉設定"><Icon name="close" /></button></div>
     <div className="settings-scroll"><fieldset className="settings-fields" disabled={!!busy}>
       <div className="section-label"><span className="section-number">01</span>選擇模型服務</div>
       <div className="provider-grid">{providers.map(provider => <button key={provider} aria-label={providerNames[provider]} className={`provider-option ${form.provider === provider ? 'selected' : ''}`} onClick={() => changeProvider(provider)} disabled={!!busy}><span className="provider-symbol">{provider === 'demo' ? <Icon name="play" size={16} /> : provider === 'custom' ? <Icon name="code" size={18} /> : providerNames[provider][0]}</span>{providerNames[provider]}{form.provider === provider && <Icon name="check" size={15} />}</button>)}</div>
       {form.provider === 'demo' ? <div className="neutral-note"><Icon name="browser" /><p>本機示範使用固定步驟操作隔離瀏覽器，驗證截圖、操作與確認流程。它不會呼叫模型，也不會操作你的桌面。</p></div> : <>
         <label className="field-label" htmlFor="model-id">模型 ID<span>可使用服務商提供的任何相容模型</span></label><input id="model-id" value={form.model} onChange={e => set('model', e.target.value)} placeholder={form.provider === 'ollama' ? '例如：qwen3-vl:2b' : '輸入完整模型 ID'} autoComplete="off" spellCheck={false} />
         {form.provider === 'ollama' && <><p className="model-loading">{modelStatus}</p><div className="model-presets">{models.filter(item => !item.name.toLowerCase().includes('ocr')).map(item => <button className="model-preset" key={item.name} onClick={() => set('model', item.name)}><Icon name="spark" size={12}/>{item.name}<span>決策模型</span></button>)}</div>{models.some(item => item.name.toLowerCase().includes('ocr')) && <p className="field-hint">{models.filter(item => item.name.toLowerCase().includes('ocr')).map(item => item.name).join('、')} 已安裝，可於下方選作 OCR 感知模型。</p>}</>}
         <label className="field-label" htmlFor="base-url">API Base URL</label><input id="base-url" type="url" value={form.base_url} onChange={e => set('base_url', e.target.value)} placeholder="https://your-endpoint.example/v1" autoComplete="off" spellCheck={false}/>
         {form.provider === 'custom' && <p className="field-hint">自訂端點需支援 OpenAI Chat Completions 格式，並回傳 JSON 動作。</p>}
         <label className="field-label" htmlFor="api-key">API Key<span>{form.provider === 'ollama' ? '本機 Ollama 通常不需要' : '只存在目前服務的記憶體中'}</span></label><div className="input-icon"><Icon name="key" size={16} /><input id="api-key" type="password" value={form.api_key || ''} onChange={e => set('api_key', e.target.value)} placeholder={config.has_key && form.provider === config.provider && form.base_url.replace(/\/$/, '') === savedEndpoint ? '已設定金鑰；留空沿用' : '輸入 API 金鑰'} autoComplete="new-password" spellCheck={false}/></div>{config.has_key && form.provider === config.provider && form.base_url.replace(/\/$/, '') === savedEndpoint && <button className="text-button clear-key-button" onClick={() => void clearKey()}>清除已儲存金鑰</button>}
       </>}
       <div className="settings-divider"/>
       <div className="section-label"><span className="section-number">02</span>讓模型理解畫面</div>
       <div className="toggle-field"><div><strong>模型支援影像（VLM）</strong><p>開啟後，將截圖連同文字觀察交給模型。</p></div><button type="button" className={`switch ${form.vision ? 'on' : ''}`} role="switch" aria-checked={form.vision} aria-label="模型支援影像" onClick={() => set('vision', !form.vision)}><span/></button></div>
       {!form.vision && <div className="text-model-note"><Icon name="code" size={18}/><p><strong>純文字模型也能操作。</strong>瀏覽器會提供 DOM 元素；桌面會以 OCR 擷取文字與座標。模型只接收文字與元素 ID，不傳送截圖。</p></div>}
       <label className="field-label" htmlFor="perception">畫面感知方式</label><select id="perception" value={form.perception} onChange={e => set('perception', e.target.value as ModelConfig['perception'])}><option value="auto">自動 · 瀏覽器 DOM／桌面 OCR</option><option value="ocr">OCR · 文字辨識與位置</option><option value="ocr_yolo">OCR + YOLO-World · 文字與物件</option></select>
       <div className="capability-line"><span className={`capability-dot ${perception?.ocr ? 'ready' : ''}`}/>OCR {perception?.ocr ? `可用${perception.ocr_engine ? ` · ${perception.ocr_engine}` : ''}` : '尚未就緒'}<span className={`capability-dot ${perception?.yolo ? 'ready' : ''}`}/>YOLO-World {perception?.yolo ? '可用' : '未載入'}</div>
       {form.perception === 'ocr_yolo' && !perception?.yolo && <div className="inline-warning"><Icon name="alert" size={17}/><span>YOLO-World 尚未就緒。需安裝選用依賴並透過 COMPUTERUSE_YOLO_MODEL 設定本機權重路徑；選擇後執行會明確回報未就緒。</span></div>}
       <p className="field-hint">OCR 和物件辨識是獨立的感知元件，決策模型本身無須具備視覺能力。</p>
       <div className="ocr-options"><label className="field-label" htmlFor="ocr-engine">OCR 引擎</label><select id="ocr-engine" value={form.ocr_engine || 'auto'} onChange={e => set('ocr_engine', e.target.value as ModelConfig['ocr_engine'])}><option value="auto">自動 · 優先原生 OCR</option><option value="native">原生 OCR · macOS Vision／RapidOCR</option><option value="glm_ocr">GLM-OCR · Ollama 感知模型</option></select>{form.ocr_engine === 'glm_ocr' && <><label className="field-label" htmlFor="ocr-model">OCR 模型 ID<span>負責辨識，不負責操作決策</span></label><input id="ocr-model" value={form.ocr_model || ''} onChange={e => set('ocr_model', e.target.value)} placeholder="glm-ocr:latest" spellCheck={false}/><label className="field-label" htmlFor="ocr-base-url">OCR Ollama 位址</label><input id="ocr-base-url" value={form.ocr_base_url || ''} onChange={e => set('ocr_base_url', e.target.value)} placeholder="http://127.0.0.1:11434" spellCheck={false}/><p className="field-hint">截圖會交給此 OCR 端點辨識，再將文字提供給決策模型。使用本機位址即可讓影像留在你的電腦；純文字模式仍不會把截圖傳給決策模型。</p></>}</div>
       <div className="settings-divider"/>
       <label className="field-label" htmlFor="max-tokens">每次回應的最大 Token</label><input id="max-tokens" type="number" min={256} max={16384} step={256} value={form.max_tokens} onChange={e => set('max_tokens', Number(e.target.value))}/>
       <div className="privacy-note"><Icon name="shield" size={18}/><p>{form.vision ? '執行時會把畫面截圖、畫面文字與任務傳至你設定的模型端點，可能包含畫面上的私人資訊。' : '執行時會把畫面辨識文字、元素座標與任務傳至決策模型端點；決策模型不接收截圖。若使用 GLM-OCR，截圖會交給獨立的 OCR 端點。'}設定與金鑰只保留在本機服務的記憶體中，重啟後需重新設定。金鑰不寫入瀏覽器儲存空間或設定檔。連線測試僅在你點擊後發出請求。</p></div>
       {feedback && <div ref={feedbackElement} role="status" className={`feedback ${feedback.ok ? 'success' : 'error'}`}><Icon name={feedback.ok ? 'check' : 'alert'} size={18}/><span>{feedback.message}</span></div>}
     </fieldset></div>
     <div className="settings-footer"><button className="button secondary" disabled={!!busy} onClick={() => void submit('test')}><Icon name="refresh" size={17} className={busy === 'test' ? 'spin' : ''}/>{busy === 'test' ? '測試連線中…' : '測試連線'}</button><button className="button primary" disabled={!!busy} onClick={() => void submit('save')}>{busy === 'save' ? '儲存中…' : '儲存設定'}<Icon name="check" size={17}/></button></div>
   </div>
 </div>;
}
