import type { ControlRow, RawIncident, SourceStatus } from './types';
import { BASE, BORDER_CAP, BORDER_REGIONS, FIXTURE_MODE, LIST_CAP, LIST_COLS, LOAD_DAYS, SB_KEY, SB_URL } from './config';
import { cutoffDate, localToEpoch, zonedParts } from './time';

const HDR = { apikey: SB_KEY, Authorization: `Bearer ${SB_KEY}` };

export class HttpError extends Error {
  constructor(public status: number, msg: string) { super(msg); }
}

async function rest<T>(path: string, init: RequestInit = {}, timeoutMs = 20_000): Promise<T> {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(`${SB_URL}/rest/v1/${path}`, {
      ...init, headers: { ...HDR, ...(init.headers || {}) }, cache: 'no-store', signal: ctl.signal,
    });
    if (!r.ok) throw new HttpError(r.status, `HTTP ${r.status}`);
    const text = await r.text();
    return (text ? JSON.parse(text) : null) as T;
  } finally { clearTimeout(t); }
}

// ── Fixture mode ────────────────────────────────────────────────────────────
let fixtureCache: RawIncident[] | null = null;

/** Loads /fixtures/incidents.json and re-bases its clock so the newest row is "now". */
export async function loadFixture(now = Date.now()): Promise<RawIncident[]> {
  if (fixtureCache) return fixtureCache;
  const r = await fetch(`${BASE}fixtures/incidents.json`, { cache: 'no-store' });
  if (!r.ok) throw new HttpError(r.status, 'fixture missing');
  const rows = (await r.json()) as RawIncident[];
  fixtureCache = rebaseFixture(rows, now);
  return fixtureCache;
}

export function rebaseFixture(rows: RawIncident[], now: number): RawIncident[] {
  let newest = 0;
  for (const r of rows) {
    if (r.category === 'weather') continue;
    const t = localToEpoch(r.occurred, r.occurred_time);
    if (t && t > newest) newest = t;
  }
  if (!newest) return rows;
  const delta = now - 12 * 60_000 - newest; // newest row lands 12 minutes ago
  const shiftIso = (s: string | null | undefined) => {
    const t = s ? Date.parse(s) : NaN;
    return Number.isFinite(t) ? new Date(t + delta).toISOString() : null;
  };
  return rows.map((r) => {
    const t = localToEpoch(r.occurred, r.occurred_time);
    if (t === null) return { ...r, first_seen: shiftIso(r.first_seen), last_seen: shiftIso(r.last_seen) };
    const p = zonedParts(t + delta);
    const p2 = (n: number) => String(n).padStart(2, '0');
    return {
      ...r,
      occurred: `${p.y}-${p2(p.m)}-${p2(p.d)}`,
      occurred_time: r.occurred_time ? `${p2(p.h)}:${p2(p.mi)}` : null,
      ts: new Date(t + delta).toISOString(),
      first_seen: shiftIso(r.first_seen), last_seen: shiftIso(r.last_seen),
    };
  });
}

// ── incidents ───────────────────────────────────────────────────────────────
export async function fetchInitial(now = Date.now()): Promise<{ rows: RawIncident[]; capped: string[] }> {
  if (FIXTURE_MODE) return { rows: await loadFixture(now), capped: [] };
  const since = cutoffDate(now, LOAD_DAYS);
  const q = (c: string) =>
    `incidents?select=${LIST_COLS}&country=eq.${c}&occurred=gte.${since}&order=ts.desc.nullslast&limit=${LIST_CAP}`;
  // The border belt is fetched separately: Lower/Upper Austria's dispatch logs
  // are so much louder than Styria and Carinthia that a single time-ordered
  // query returned no border rows at all once the cap was reached.
  const border =
    `incidents?select=${LIST_COLS}&country=eq.AT&region=in.(${BORDER_REGIONS.join(',')})`
    + `&occurred=gte.${since}&order=ts.desc.nullslast&limit=${BORDER_CAP}`;
  const [at, hr, bd] = await Promise.all([
    rest<RawIncident[]>(q('AT')), rest<RawIncident[]>(q('HR')), rest<RawIncident[]>(border),
  ]);
  const capped: string[] = [];
  if (at.length >= LIST_CAP) capped.push('AT');
  if (hr.length >= LIST_CAP) capped.push('HR');
  const seen = new Set<string>();
  const rows: RawIncident[] = [];
  for (const r of [...at, ...hr, ...bd]) {
    if (seen.has(r.id)) continue;
    seen.add(r.id);
    rows.push(r);
  }
  return { rows, capped };
}

/** Rows touched since `maxSeen` (poller upserts move last_seen). */
export async function fetchIncremental(maxSeen: string, now = Date.now()): Promise<RawIncident[]> {
  if (FIXTURE_MODE) return [];
  if (!maxSeen) return (await fetchInitial(now)).rows;
  const since = cutoffDate(now, LOAD_DAYS);
  return rest<RawIncident[]>(
    `incidents?select=${LIST_COLS}&last_seen=gt.${encodeURIComponent(maxSeen)}&occurred=gte.${since}&order=last_seen.asc&limit=1000`,
  );
}

export async function fetchRaw(id: string): Promise<{ raw: string | null; raw_sl: string | null } | null> {
  if (FIXTURE_MODE) {
    const r = (fixtureCache || []).find((x) => x.id === id);
    return r ? { raw: r.raw ?? null, raw_sl: r.raw_sl ?? null } : null;
  }
  const rows = await rest<{ raw: string | null; raw_sl: string | null }[]>(
    `incidents?select=raw,raw_sl&id=eq.${encodeURIComponent(id)}&limit=1`,
  );
  return rows[0] ?? null;
}

// ── control / poller ────────────────────────────────────────────────────────
export async function readControl(): Promise<ControlRow | null> {
  if (FIXTURE_MODE) return null;
  const rows = await rest<ControlRow[]>(
    'control?id=eq.1&select=id,poll_requested_at,poll_started_at,poll_finished_at,runner_until,note', {}, 10_000,
  );
  return rows[0] ?? null;
}

export async function requestPoll(): Promise<boolean> {
  if (FIXTURE_MODE) return false;
  await rest('rpc/request_poll', { method: 'POST', body: '{}', headers: { 'Content-Type': 'application/json' } }, 10_000);
  return true;
}

export const runnerUp = (c: ControlRow | null, now = Date.now()): boolean =>
  !!(c && c.runner_until && Date.parse(c.runner_until) > now);

// ── source health (optional table) ──────────────────────────────────────────
let sourceStatusMissing = false;
export async function fetchSourceStatus(): Promise<SourceStatus[] | null> {
  if (FIXTURE_MODE || sourceStatusMissing) return null;
  try {
    return await rest<SourceStatus[]>('source_status?select=source,ok,rows,ms,error,checked_at&order=source.asc&limit=200', {}, 10_000);
  } catch (e) {
    if (e instanceof HttpError && (e.status === 404 || e.status === 401 || e.status === 403)) sourceStatusMissing = true;
    return null;
  }
}
