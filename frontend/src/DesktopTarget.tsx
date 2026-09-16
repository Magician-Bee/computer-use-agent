import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { api } from './api';
import Icon from './Icon';
import type { Capabilities, DesktopSelection, DesktopWindows, Health } from './types';

type Props = {
  capabilities?: Capabilities;
  selection: DesktopSelection | null;
  onChange: Dispatch<SetStateAction<DesktopSelection | null>>;
  onAvailability: Dispatch<SetStateAction<boolean>>;
  onHealth: Dispatch<SetStateAction<Health | null>>;
};
const key = (value: DesktopSelection) => `${value.pid}:${value.window_id}`;

export default function DesktopTarget({ capabilities, selection, onChange, onAvailability, onHealth }: Props) {
  const [data, setData] = useState<DesktopWindows | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const request = useRef<AbortController | null>(null);
  const backend = capabilities?.desktop_backend;
  const available = capabilities?.desktop === true && data?.available === true;
  const chosen = data?.windows.find(window => selection && key(window) === key(selection));

  const refresh = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true); setError(''); onAvailability(false);
    try {
      const [next, nextHealth] = await Promise.all([
        api<DesktopWindows>('/desktop/windows', undefined, controller.signal),
        api<Health>('/health', undefined, controller.signal),
      ]);
      if (controller.signal.aborted) return;
      setData(next);
      onHealth(nextHealth);
      setError(next.error || '');
      onAvailability(next.available === true);
      onChange(current => current && next.windows.some(window => key(window) === key(current)) ? current : null);
    } catch (problem) {
      if (controller.signal.aborted) return;
      setData(null); onChange(null);
      setError(problem instanceof Error ? problem.message : '無法讀取視窗清單。');
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [onAvailability, onChange, onHealth]);

  useEffect(() => {
    void refresh();
    return () => { request.current?.abort(); onAvailability(false); };
  }, [refresh, onAvailability]);

  return <div className="desktop-target">
    <div className="desktop-target-heading"><div><Icon name="desktop" size={17}/><strong>桌面目標與能力</strong></div><button className="text-button" disabled={loading} onClick={() => void refresh()} aria-label="重新讀取視窗清單"><Icon name="refresh" size={14} className={loading ? 'spin' : ''}/>{loading ? '讀取中' : '重新整理'}</button></div>
    <p className="field-hint">只讀取視窗清單，不切換焦點或操作程式。請明確選擇本次目標。</p>
    <label htmlFor="desktop-window">目標應用程式與視窗</label>
    <select id="desktop-window" value={selection ? key(selection) : ''} disabled={loading || !data?.windows.length} onChange={event => {
      const window = data?.windows.find(item => key(item) === event.target.value);
      onChange(window ? { pid: window.pid, window_id: window.window_id } : null);
    }}>
      <option value="">{loading ? '讀取視窗清單…' : data?.windows.length ? '選擇一個視窗' : '目前沒有可列出的視窗'}</option>
      {data?.windows.map(window => <option key={key(window)} value={key(window)}>{window.app_name || '未知應用程式'} · {window.title || '未命名視窗'} · #{window.window_id}</option>)}
    </select>
    {chosen && <p className="window-identity">PID {chosen.pid} · 視窗 #{chosen.window_id}{chosen.is_on_screen !== undefined ? ` · ${chosen.is_on_screen ? '畫面中可見' : '目前不在畫面中'}` : ''}</p>}
    <dl className="desktop-diagnostics">
      <div><dt>背景驅動</dt><dd>{backend?.backend ? `${backend.backend}${backend.version ? ` ${backend.version}` : ''}` : '尚未回報'}<span className={available ? 'diagnostic-ready' : ''}>{available ? '可用' : '未就緒'}</span></dd></div>
      <div><dt>使用者輸入隔離</dt><dd>{backend?.physical_input_untouched === true ? '驅動已回報支援' : '尚未通過驗證'}</dd></div>
      <div><dt>Agent 自己的游標</dt><dd>{backend?.agent_cursor_available === true ? '驅動已回報支援' : '尚未通過驗證'}</dd></div>
    </dl>
    {(error || !available) && <p className="desktop-unavailable" role="status"><Icon name="alert" size={16}/><span>{error || backend?.reason || capabilities?.desktop_hint || '背景獨立輸入尚未就緒，桌面任務目前停用。'}</span></p>}
    <p className="field-hint">桌面任務要求 Agent 有獨立輸入，不能搶走你的實體滑鼠、鍵盤或焦點。「顯示獨立瀏覽器視窗」只控制瀏覽器是否顯示，並不代表桌面背景輸入已通過驗證。</p>
  </div>;
}
