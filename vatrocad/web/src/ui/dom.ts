export const $ = <T extends HTMLElement = HTMLElement>(id: string): T => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`#${id} missing`);
  return el as T;
};

export const esc = (s: unknown): string =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string));

export const isPhone = (): boolean => window.innerWidth < 1024;

export const reducedMotion = (): boolean =>
  typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches;

let toastTimer: number | undefined;
export function toast(msg: string, ms = 2800): void {
  const t = document.getElementById('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('on');
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => t.classList.remove('on'), ms);
}

export function tryStorage<T>(fn: () => T, fallback: T): T {
  try { return fn(); } catch { return fallback; }
}

export const debounce = <A extends unknown[]>(fn: (...a: A) => void, ms: number) => {
  let t: number | undefined;
  return (...a: A) => { window.clearTimeout(t); t = window.setTimeout(() => fn(...a), ms); };
};
