import type { Category, Incident, Kind } from './types';
import { AT_BORDER, CATEGORIES, KINDS, RADII, WINDOWS } from './labels';
import { fold } from './classify';
import { haversineKm, type LatLon } from './geo';
import { timeBand, BAND_LABEL, type Band } from './time';

export type CountryFilter = 'all' | 'AT' | 'HR';
export type StatusFilter = 'all' | 'active' | 'contained' | 'closed' | 'report';

export interface Filters {
  c: CountryFilter;
  r: string;             // region code or 'all'
  k: Kind | 'all';
  cat: Category | 'all';
  s: StatusFilter;
  w: number;             // window in hours
  q: string;             // raw query text
  b: boolean;            // AT border belt only
  d: number;             // near-me radius km, 0 = off
  t: TagFilter;          // helicopter / EMS mention
}
export type TagFilter = 'all' | 'heli' | 'ems';

export const DEFAULT_FILTERS: Filters = {
  c: 'all', r: 'all', k: 'all', cat: 'all', s: 'all', w: 24, q: '', b: false, d: 0, t: 'all',
};

const WINDOW_SET = new Set(WINDOWS.map((w) => w.h));
const KIND_SET = new Set<string>(KINDS);
const CAT_SET = new Set<string>(CATEGORIES);
const STATUS_SET = new Set<string>(['all', 'active', 'contained', 'closed', 'report']);

/** Parse a `#c=AT&w=24&q=...` hash (or a query-string-like object) into Filters + extras. */
export function parseHash(hash: string): { f: Partial<Filters>; id?: string; v?: 'list' | 'map' } {
  const p = new URLSearchParams(hash.replace(/^#/, ''));
  const f: Partial<Filters> = {};
  const c = p.get('c'); if (c === 'AT' || c === 'HR' || c === 'all') f.c = c;
  const r = p.get('r'); if (r && /^[a-z]{2,5}$|^all$/.test(r)) f.r = r;
  const k = p.get('k'); if (k && (k === 'all' || KIND_SET.has(k))) f.k = k as Kind | 'all';
  const cat = p.get('cat'); if (cat && (cat === 'all' || CAT_SET.has(cat))) f.cat = cat as Category | 'all';
  const s = p.get('s'); if (s && STATUS_SET.has(s)) f.s = s as StatusFilter;
  const w = Number(p.get('w')); if (WINDOW_SET.has(w)) f.w = w;
  const q = p.get('q'); if (q !== null) f.q = q.slice(0, 120);
  const b = p.get('b'); if (b !== null) f.b = b === '1';
  if (p.has('d')) { const d = Number(p.get('d')); if (d === 0 || RADII.includes(d)) f.d = d; }
  const t = p.get('t'); if (t === 'all' || t === 'heli' || t === 'ems') f.t = t;
  const id = p.get('id') || undefined;
  const v = p.get('v'); const view = v === 'map' ? 'map' : v === 'list' ? 'list' : undefined;
  return { f, id, ...(view ? { v: view } : {}) };
}

/** Serialise only the non-default parts so links stay short. */
export function toHash(f: Filters, extra: { id?: string | null; v?: 'list' | 'map' } = {}): string {
  const p = new URLSearchParams();
  if (f.c !== DEFAULT_FILTERS.c) p.set('c', f.c);
  if (f.r !== DEFAULT_FILTERS.r) p.set('r', f.r);
  if (f.k !== DEFAULT_FILTERS.k) p.set('k', f.k);
  if (f.cat !== DEFAULT_FILTERS.cat) p.set('cat', f.cat);
  if (f.s !== DEFAULT_FILTERS.s) p.set('s', f.s);
  if (f.w !== DEFAULT_FILTERS.w) p.set('w', String(f.w));
  if (f.q) p.set('q', f.q);
  if (f.b) p.set('b', '1');
  if (f.d) p.set('d', String(f.d));
  if (f.t !== 'all') p.set('t', f.t);
  if (extra.v === 'map') p.set('v', 'map');
  if (extra.id) p.set('id', extra.id);
  const s = p.toString();
  return s ? '#' + s : '';
}

export function normaliseFilters(p: Partial<Filters>): Filters {
  return { ...DEFAULT_FILTERS, ...p };
}

export function activeFilterCount(f: Filters): number {
  let n = 0;
  if (f.r !== 'all') n++;
  if (f.k !== 'all') n++;
  if (f.cat !== 'all') n++;
  if (f.s !== 'all') n++;
  if (f.w !== DEFAULT_FILTERS.w) n++;
  if (f.b) n++;
  if (f.d) n++;
  if (f.t !== 'all') n++;
  return n;
}

export interface FilterCtx {
  now: number;
  pos?: LatLon | null;
}

/** Hidden by default: nothing. Rows of every kind are shown; the kind filter narrows. */
function matchCountry(i: Incident, f: Filters): boolean {
  return f.c === 'all' || i.country === f.c;
}
function matchRegion(i: Incident, f: Filters): boolean {
  return f.r === 'all' || i.region === f.r;
}
function matchKind(i: Incident, f: Filters): boolean {
  return f.k === 'all' || i.kind === f.k;
}
function matchCat(i: Incident, f: Filters): boolean {
  return f.cat === 'all' || i.cat === f.cat;
}
function matchStatus(i: Incident, f: Filters): boolean {
  switch (f.s) {
    case 'all': return true;
    case 'active': return i.active;
    case 'contained': return i.st === 'contained';
    case 'closed': return i.st === 'closed' && i.kind === 'dispatch';
    case 'report': return i.kind === 'report' || i.kind === 'police';
  }
}
function matchWindow(i: Incident, f: Filters, ctx: FilterCtx): boolean {
  const t = i.epoch ?? i.firstSeenMs;
  if (t > ctx.now + 5 * 60_000) return i.cat === 'weather'; // future-dated: only weather is legitimate
  return ctx.now - t <= f.w * 3_600_000;
}
function matchBorder(i: Incident, f: Filters): boolean {
  if (!f.b) return true;
  return i.country !== 'AT' || AT_BORDER.has(i.region || '');
}
function matchNear(i: Incident, f: Filters, ctx: FilterCtx): boolean {
  if (!f.d || !ctx.pos) return true;
  if (i.lat === null || i.lon === null) return false;
  return haversineKm(ctx.pos, { lat: i.lat, lon: i.lon }) <= f.d;
}
function matchQuery(i: Incident, q: string): boolean {
  if (!q) return true;
  return i.hay.includes(q);
}
function matchTag(i: Incident, f: Filters): boolean {
  return f.t === 'all' || (f.t === 'heli' ? i.heli : i.ems);
}

export function matches(i: Incident, f: Filters, ctx: FilterCtx, q = fold(f.q.trim())): boolean {
  return matchCountry(i, f) && matchRegion(i, f) && matchKind(i, f) && matchCat(i, f)
    && matchStatus(i, f) && matchWindow(i, f, ctx) && matchBorder(i, f) && matchNear(i, f, ctx)
    && matchTag(i, f) && matchQuery(i, q);
}

export function applyFilters(rows: Iterable<Incident>, f: Filters, ctx: FilterCtx): Incident[] {
  const q = fold(f.q.trim());
  const out: Incident[] = [];
  for (const i of rows) if (matches(i, f, ctx, q)) out.push(i);
  return out;
}

/** Active first (status active before contained), then newest first; rows
 *  without a known time sort by first_seen. Deterministic tie-break on id. */
export function compareIncidents(a: Incident, b: Incident): number {
  const ra = rank(a), rb = rank(b);
  if (ra !== rb) return ra - rb;
  if (a.sortMs !== b.sortMs) return b.sortMs - a.sortMs;
  return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}
function rank(i: Incident): number {
  if (i.cat === 'exercise') return 4;
  if (i.cat === 'weather') return 3;
  if (i.st === 'active') return 0;
  if (i.st === 'contained') return 1;
  return 2;
}
export function sortIncidents(rows: Incident[]): Incident[] {
  return rows.slice().sort(compareIncidents);
}

export interface Group { key: Band; label: string; items: Incident[] }
const BAND_ORDER: Band[] = ['hour', 'today', 'yesterday', 'older', 'future', 'weather'];

/** Group an already sorted list by time band, keeping order inside each band.
 *  Active rows are pulled into the top band so they stay on top; weather
 *  warnings (context, often future-dated) form their own last band. */
export function groupByBand(rows: Incident[], now: number): Group[] {
  const map = new Map<Band, Incident[]>();
  for (const i of rows) {
    let band: Band = i.cat === 'weather' ? 'weather' : timeBand(i.epoch ?? i.firstSeenMs, now);
    if (i.active) band = 'hour';
    let arr = map.get(band);
    if (!arr) map.set(band, (arr = []));
    arr.push(i);
  }
  const out: Group[] = [];
  for (const b of BAND_ORDER) {
    const items = map.get(b);
    if (items && items.length) out.push({ key: b, label: BAND_LABEL[b], items });
  }
  return out;
}

export interface FacetCounts {
  c: Record<string, number>;
  r: Record<string, number>;
  k: Record<string, number>;
  cat: Record<string, number>;
  s: Record<string, number>;
  w: Record<string, number>;
  b: number;
  d: Record<string, number>;
  t: Record<string, number>;
}

/** For every chip: how many rows would show if that chip alone were changed. */
export function facetCounts(rows: Iterable<Incident>, f: Filters, ctx: FilterCtx): FacetCounts {
  const q = fold(f.q.trim());
  const out: FacetCounts = { c: {}, r: {}, k: {}, cat: {}, s: {}, w: {}, b: 0, d: {}, t: {} };
  const inc = (o: Record<string, number>, k: string) => { o[k] = (o[k] || 0) + 1; };
  const statusKeys: StatusFilter[] = ['all', 'active', 'contained', 'closed', 'report'];
  for (const i of rows) {
    const mc = matchCountry(i, f), mr = matchRegion(i, f), mk = matchKind(i, f), mcat = matchCat(i, f),
      ms = matchStatus(i, f), mw = matchWindow(i, f, ctx), mb = matchBorder(i, f), mn = matchNear(i, f, ctx),
      mt = matchTag(i, f), mq = matchQuery(i, q);
    const rest = (skip: string) =>
      (skip === 'c' || mc) && (skip === 'r' || mr) && (skip === 'k' || mk) && (skip === 'cat' || mcat)
      && (skip === 's' || ms) && (skip === 'w' || mw) && (skip === 'b' || mb) && (skip === 'd' || mn)
      && (skip === 't' || mt) && mq;
    if (rest('t')) { inc(out.t, 'all'); if (i.heli) inc(out.t, 'heli'); if (i.ems) inc(out.t, 'ems'); }
    if (rest('c')) { inc(out.c, 'all'); inc(out.c, i.country); }
    if (rest('r') && i.region) { inc(out.r, 'all'); inc(out.r, i.region); }
    if (rest('k')) { inc(out.k, 'all'); inc(out.k, i.kind); }
    if (rest('cat')) { inc(out.cat, 'all'); inc(out.cat, i.cat); }
    if (rest('s')) for (const s of statusKeys) if (matchStatus(i, { ...f, s })) inc(out.s, s);
    if (rest('w')) for (const w of WINDOWS) if (matchWindow(i, { ...f, w: w.h }, ctx)) inc(out.w, String(w.h));
    if (rest('b') && matchBorder(i, { ...f, b: true })) out.b++;
    if (rest('d') && ctx.pos) for (const d of RADII) if (matchNear(i, { ...f, d }, ctx)) inc(out.d, String(d));
  }
  return out;
}

/** 24 hourly buckets (index 0 = the current hour, 23 = 23 h ago) for the histogram. */
export function hourHistogram(rows: Iterable<Incident>, now: number): number[] {
  const bins = new Array<number>(24).fill(0);
  for (const i of rows) {
    const t = i.epoch ?? i.firstSeenMs;
    const h = Math.floor((now - t) / 3_600_000);
    if (h >= 0 && h < 24) bins[h]!++;
  }
  return bins;
}
