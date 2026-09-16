import type { CSSProperties } from 'react';
export type IconName = 'spark' | 'plus' | 'settings' | 'arrow' | 'browser' | 'desktop' | 'chevron' | 'close' | 'check' | 'stop' | 'download' | 'eye' | 'cursor' | 'code' | 'shield' | 'clock' | 'refresh' | 'menu' | 'help' | 'expand' | 'key' | 'link' | 'play' | 'alert' | 'terminal' | 'layers';
const paths: Record<IconName, React.ReactNode> = {
 spark: <><path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3Z"/><path d="m20 2 .6 1.4L22 4l-1.4.6L20 6l-.6-1.4L18 4l1.4-.6L20 2Z"/></>,
 plus: <path d="M12 5v14M5 12h14"/>, settings: <><path d="m9 3-.5 2.3-2 .9-2.1-.6-2 3.4 1.6 1.7v2.6L2.4 15l2 3.4 2.1-.6 2 .9L9 21h4l.5-2.3 2-.9 2.1.6 2-3.4-1.6-1.7v-2.6L19.6 9l-2-3.4-2.1.6-2-.9L13 3Z"/><circle cx="11" cy="12" r="3"/></>,
 arrow: <path d="M5 12h14m-6-6 6 6-6 6"/>, browser: <><rect x="3" y="4" width="18" height="16" rx="3"/><path d="M3 9h18M7 6.5h.01M10 6.5h.01"/></>, desktop: <><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/></>,
 chevron: <path d="m8 10 4 4 4-4"/>, close: <path d="m6 6 12 12M6 18 18 6"/>, check: <path d="m5 12 4 4L19 6"/>, stop: <rect x="6" y="6" width="12" height="12" rx="2"/>, download: <><path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/></>,
 eye: <><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></>, cursor: <path d="m5 3 4 17 3-7 7-3L5 3Z"/>, code: <><path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18"/></>, shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/></>,
 clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>, refresh: <><path d="M20 11a8 8 0 0 0-14-5L3 9m0-5v5h5M4 13a8 8 0 0 0 14 5l3-3m0 5v-5h-5"/></>, menu: <path d="M4 6h16M4 12h16M4 18h16"/>, help: <><circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 0 1 5 .2c0 1.6-2.5 2-2.5 4M12 17h.01"/></>,
 expand: <path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>, key: <><circle cx="8" cy="8" r="5"/><path d="m12 12 9 9m-3-3 3-3m-6 0 3-3"/></>, link: <><path d="m9 15 6-6m-5-3 2-2a5 5 0 0 1 7 7l-2 2M7 11l-2 2a5 5 0 0 0 7 7l2-2"/></>, play: <path d="m8 4 12 8-12 8V4Z"/>, alert: <><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3h.01"/></>, terminal: <><rect x="2" y="3" width="20" height="18" rx="3"/><path d="m6 8 4 4-4 4m7 0h5"/></>, layers: <><path d="m12 3 10 6-10 6L2 9l10-6Zm-9 11 9 5 9-5m-18 4 9 5 9-5"/></>,
};
export default function Icon({ name, size = 20, className, style }: { name: IconName; size?: number; className?: string; style?: CSSProperties }) {
 return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className={className} style={style}>{paths[name]}</svg>;
}
