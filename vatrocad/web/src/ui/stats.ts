import type { Incident, SourceStatus } from '../types';
import { hourHistogram } from '../filters';
import { fmtDateTime, relTime } from '../time';
import { esc } from './dom';
import { ic } from './sprite';

export interface StatsInput {
  shown: Incident[];
  all: Iterable<Incident>;
  now: number;
}

/** Stats strip: totals, active, per-country and a 24-h histogram (inline SVG). */
export function renderStats(root: HTMLElement, s: StatsInput): void {
  let at = 0, hr = 0, active = 0;
  for (const i of s.shown) { if (i.country === 'AT') at++; else hr++; if (i.active) active++; }
  const bins = hourHistogram(s.shown, s.now);
  const max = Math.max(1, ...bins);
  const W = 24 * 5, H = 22;
  const bars = bins.map((n, h) => {
    const x = (23 - h) * 5; const bh = Math.max(n ? 2 : 0, Math.round((n / max) * H));
    return `<rect x="${x}" y="${H - bh}" width="4" height="${bh}" rx="1" class="${h === 0 ? 'cur' : ''}"><title>${h === 0 ? 'zadnja ura' : `pred ${h} h`}: ${n}</title></rect>`;
  }).join('');
  root.innerHTML = `
    <span class="st"><b>${s.shown.length}</b> prikazanih</span>
    <span class="st ${active ? 'hot' : ''}">${ic('bolt')}<b>${active}</b> aktivnih</span>
    <span class="st"><b>${at}</b> AT</span>
    <span class="st"><b>${hr}</b> HR</span>
    <svg class="hist" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Število dogodkov po urah, zadnjih 24 ur">${bars}</svg>
    <button type="button" class="st btn-src" id="srcBtn" aria-haspopup="dialog">${ic('radio')} Viri</button>`;
}

export interface SourceHealthRow {
  name: string; country: 'AT' | 'HR'; count: number; newest: number; ok: boolean | null; error: string | null; checked: number | null;
}

/** Health per source from source_status when present, else from the newest row we hold. */
export function sourceHealth(rows: Iterable<Incident>, status: SourceStatus[] | null, now: number): SourceHealthRow[] {
  const by = new Map<string, SourceHealthRow>();
  for (const i of rows) {
    let s = by.get(i.source);
    if (!s) by.set(i.source, (s = { name: i.source, country: i.country, count: 0, newest: 0, ok: null, error: null, checked: null }));
    s.count++;
    const t = i.epoch ?? i.firstSeenMs;
    if (t > s.newest && t <= now + 3_600_000) s.newest = t;
  }
  if (status) for (const st of status) {
    let s = by.get(st.source);
    if (!s) by.set(st.source, (s = { name: st.source, country: /Feuerwehr|LFV|BFKDO|Polizei|Policija .* LPD|Avstrij/.test(st.source) ? 'AT' : 'HR', count: st.rows ?? 0, newest: 0, ok: null, error: null, checked: null }));
    s.ok = st.ok; s.error = st.error; s.checked = st.checked_at ? Date.parse(st.checked_at) : null;
  }
  return [...by.values()].sort((a, b) => tier(a, now) - tier(b, now) || b.newest - a.newest);
}

function tier(s: SourceHealthRow, now: number): number {
  if (s.ok === false) return 0;
  const age = now - s.newest;
  return age <= 12 * 3_600_000 ? 1 : age <= 3 * 86_400_000 ? 2 : 3;
}

export function sourcesHTML(list: SourceHealthRow[], now: number): string {
  if (!list.length) return '<div class="empty">Ni podatkov o virih.</div>';
  const lab = ['NAPAKA', 'V ŽIVO', 'NEDAVNO', 'STARO'];
  const cls = ['bad', 'ok', 'mid', 'old'];
  return `<div class="srcgrid">${list.map((s) => {
    const t = tier(s, now);
    return `<div class="src ${cls[t]}"><div class="n"><span class="cc">${s.country}</span>${esc(s.name)}</div>
      <div class="s">● ${lab[t]} · ${s.count}</div>
      <div class="m">${s.newest ? `zadnji dogodek ${esc(fmtDateTime(s.newest))} (${esc(relTime(s.newest, now))})` : 'brez dogodkov'}${s.checked ? ` · preverjeno ${esc(relTime(s.checked, now))}` : ''}${s.error ? `<br><span class="err">${esc(s.error)}</span>` : ''}</div></div>`;
  }).join('')}</div>`;
}
