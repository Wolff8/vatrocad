import './styles.css';
import type { ControlRow, Incident, SourceStatus } from './types';
import { BASE, FIXTURE_MODE, FULL_RELOAD_MS, HEAD_CHECK_MS, LIST_CAP, POLL_STEP_MS, POLL_WAIT_MS, REFRESH_THROTTLE_MS, RT_DEBOUNCE_MS, SB_KEY, SB_URL, TICK_MS } from './config';
import { COUNTRY_LABEL, T, WINDOWS } from './labels';
import { Store, type ChangeSet } from './store';
import { fetchIncremental, fetchInitial, fetchRaw, fetchSourceStatus, readControl, requestPoll, runnerUp } from './data';
import { DEFAULT_FILTERS, applyFilters, facetCounts, groupByBand, normaliseFilters, parseHash, sortIncidents, toHash, type Filters } from './filters';
import { fmtClock, minutesSince, parseIso, tzAbbrev } from './time';
import { SPRITE, ic } from './ui/sprite';
import { $, debounce, esc, isPhone, toast, tryStorage } from './ui/dom';
import { Sheet } from './ui/sheet';
import { IncidentList } from './ui/list';
import { detailHTML, shareText } from './ui/detail';
import { IncidentMap } from './ui/map';
import { renderStats, sourceHealth, sourcesHTML } from './ui/stats';
import { panelHTML, railHTML } from './ui/filtersUi';
import { Notifier } from './notify';
import type { LatLon } from './geo';

type RtStatus = 'connecting' | 'live' | 'polling' | 'offline';
const LS_FILTERS = 'vatrocad.filters.v2';

// ── State ────────────────────────────────────────────────────────────────────
const store = new Store();
const notifier = new Notifier();
let F: Filters = normaliseFilters({ ...tryStorage(() => JSON.parse(localStorage.getItem(LS_FILTERS) || '{}') as Partial<Filters>, {}), q: '', d: 0 });
let view: 'list' | 'map' = 'list';
let selected: string | null = null;
let pendingSelect: string | null = null;
let shownCap = LIST_CAP;
let pos: LatLon | null = null;
let geoBusy = false;
let rtStatus: RtStatus = 'connecting';
let control: ControlRow | null = null;
let lastControlFinished = '';
let lastSync = 0;
let sourceStatus: SourceStatus[] | null = null;
let firstLoadDone = false;
let refreshing = false;
let lastRefreshAt = 0;
let inflight: Promise<ChangeSet> | null = null;
let fitNext = true;
let sbDownToasted = false;

// ── DOM ──────────────────────────────────────────────────────────────────────
document.body.insertAdjacentHTML('afterbegin', SPRITE);
const listEl = $('list'), railEl = $('rail'), statsEl = $('stats'), segEl = $('seg'), qEl = $<HTMLInputElement>('q');
const mapEl = $('map'), backBtn = $<HTMLButtonElement>('backToList'), refreshBtn = $<HTMLButtonElement>('refreshBtn');
const ledEl = $('led'), ledTxt = $('ledTxt'), clockEl = $('clock'), notifyBtn = $<HTMLButtonElement>('notifyBtn');
const bk = $('bk');
const detailSheet = new Sheet($('sheet'), bk);
const filterSheet = new Sheet($('fsheet'), bk);
const sourceSheet = new Sheet($('ssheet'), bk);

const list = new IncidentList(listEl, {
  onSelect: (id, el) => select(id, el),
  onShowMap: (id) => showOnMap(id),
  onMore: () => { shownCap += LIST_CAP; render(); },
});
const map = new IncidentMap(mapEl, (id, explicit) => select(id, null, { fromMap: true, quiet: !explicit && isPhone() }));

// ── Rendering ────────────────────────────────────────────────────────────────
let raf = 0;
function render(): void {
  if (raf) return;
  raf = requestAnimationFrame(() => { raf = 0; renderNow(); });
}

function renderNow(): void {
  const now = Date.now();
  const ctx = { now, pos };
  const rows = store.rows.values();
  const filtered = sortIncidents(applyFilters(rows, F, ctx));
  const capped = filtered.slice(0, shownCap);
  const groups = groupByBand(capped, now);
  const w = WINDOWS.find((x) => x.h === F.w);
  const emptyText = filtered.length ? null
    : `<b>${T.empty}</b><br>${F.w < 168 ? T.emptyHint(w?.label ?? `${F.w} h`) : ''}`;
  try { list.render(groups, now, filtered.length - capped.length, emptyText); }
  catch (e) { console.error('list render failed', e); }
  try { map.sync(filtered, now, fitNext); fitNext = false; }
  catch (e) { console.error('map sync failed', e); }
  try { renderStats(statsEl, { shown: filtered, all: store.rows.values(), now }); }
  catch (e) { console.error('stats failed', e); }
  const counts = facetCounts(store.rows.values(), F, ctx);
  railEl.innerHTML = railHTML(F, counts);
  for (const b of segEl.querySelectorAll<HTMLButtonElement>('button')) {
    const c = b.dataset.c!;
    b.setAttribute('aria-pressed', String(F.c === c));
    const n = b.querySelector('.n'); if (n) n.textContent = String(counts.c[c] ?? 0);
  }
  if (filterSheet.isOpen()) filterSheet.setContent(panelHTML(F, counts, !!pos, geoBusy));
  document.title = `${filtered.filter((i) => i.active).length ? `(${filtered.filter((i) => i.active).length}) ` : ''}${T.title}`;
  if (pendingSelect && store.rows.has(pendingSelect)) { const id = pendingSelect; pendingSelect = null; select(id, null, { keepView: true }); }
}

function syncHeader(): void {
  const now = Date.now();
  const mode = FIXTURE_MODE ? 'demo podatki' : T[rtStatus];
  let poll = '';
  if (control?.poll_finished_at) {
    const fin = parseIso(control.poll_finished_at);
    poll = fin ? ` · ${T.lastPoll(minutesSince(fin, now))}` : '';
  }
  const runner = control ? ` · ${runnerUp(control, now) ? T.pollerRunning : T.pollerStopped}` : '';
  ledTxt.textContent = `${mode}${poll}${runner}`;
  ledEl.classList.toggle('b', rtStatus === 'offline');
  ledEl.classList.toggle('p', rtStatus === 'polling' || rtStatus === 'connecting');
  ledEl.classList.toggle('idle', !!control && !runnerUp(control, now));
  ledEl.title = lastSync ? `zadnja sinhronizacija ${fmtClock(lastSync)} ${tzAbbrev(lastSync)}` : '';
}

function tickClock(): void {
  const now = Date.now();
  clockEl.textContent = `${fmtClock(now)} ${tzAbbrev(now)}`;
}

// ── Selection / detail ───────────────────────────────────────────────────────
/** quiet: highlight only (a popup opened on a phone) – the detail sheet stays closed. */
function select(id: string, opener: HTMLElement | null, opts: { fromMap?: boolean; keepView?: boolean; quiet?: boolean } = {}): void {
  const i = store.rows.get(id);
  if (!i) return;
  selected = id;
  list.setSelected(id);
  map.setSelected(id, store.rows);
  writeHash();
  if (opts.quiet) return;
  renderDetail(i);
  detailSheet.show(opener ?? list.rowEl(id) ?? null);
  if (!i.hasRaw) {
    fetchRaw(id).then((r) => {
      const cur = store.attachRaw(id, r?.raw ?? null, r?.raw_sl ?? null);
      if (cur && selected === id) renderDetail(cur);
    }).catch(() => { const cur = store.rows.get(id); if (cur && selected === id) { cur.hasRaw = true; renderDetail(cur); } });
  }
  if (!opts.fromMap && !isPhone() && i.lat !== null) map.focus(i);
  if (!opts.fromMap && !opts.keepView && !isPhone()) map.reveal();
}

function renderDetail(i: Incident): void {
  detailSheet.setContent(detailHTML(i, Date.now(), { loadingRaw: !i.hasRaw }));
}

detailSheet.onClose = () => {
  if (selected) { list.setSelected(null); map.setSelected(null, store.rows); selected = null; writeHash(); }
};

detailSheet.el.addEventListener('click', async (e) => {
  const t = e.target as HTMLElement;
  const m = t.closest<HTMLElement>('[data-map]'); if (m) { showOnMap(m.dataset.map!); return; }
  const s = t.closest<HTMLElement>('[data-share]'); if (s) { await share(s.dataset.share!); }
});

async function share(id: string): Promise<void> {
  const i = store.rows.get(id); if (!i) return;
  const url = `${location.origin}${location.pathname}${toHash(F, { id })}`;
  const text = shareText(i, Date.now());
  const nav = navigator as Navigator & { share?: (d: ShareData) => Promise<void> };
  if (nav.share) { try { await nav.share({ title: T.title, text, url }); return; } catch { /* cancelled → fall through */ } }
  try { await navigator.clipboard.writeText(`${text}\n${url}`); toast(T.copied); }
  catch { toast(url); }
}

function showOnMap(id: string): void {
  const i = store.rows.get(id); if (!i || i.lat === null) return;
  if (selected !== id) { selected = id; list.setSelected(id); map.setSelected(id, store.rows); }
  if (isPhone()) { detailSheet.close(); setView('map'); }
  else map.reveal();
  window.setTimeout(() => map.focus(i), isPhone() ? 120 : 0);
  writeHash();
}

function setView(v: 'list' | 'map'): void {
  view = v;
  document.body.dataset.view = v;
  for (const b of document.querySelectorAll<HTMLButtonElement>('.view button')) b.setAttribute('aria-selected', String(b.dataset.v === v));
  backBtn.hidden = !(v === 'map' && isPhone());
  if (v === 'map') window.setTimeout(() => { map.reveal(); map.flushPending(); }, 40);
  writeHash();
}

// ── URL hash + persistence ───────────────────────────────────────────────────
function writeHash(): void {
  const h = toHash(F, { id: selected, v: view });
  const url = `${location.pathname}${location.search}${h}`;
  if (`${location.pathname}${location.search}${location.hash}` !== url) history.replaceState(history.state, '', url);
  tryStorage(() => localStorage.setItem(LS_FILTERS, JSON.stringify({ ...F, q: '', d: 0 })), undefined);
}

function readHash(): void {
  const { f, id, v } = parseHash(location.hash);
  F = normaliseFilters({ ...F, ...f });
  if (v) view = v;
  if (id) pendingSelect = id;
  qEl.value = F.q;
}

function setFilter<K extends keyof Filters>(k: K, v: Filters[K]): void {
  if (F[k] === v) return;
  F = { ...F, [k]: v };
  if (k === 'c') F.r = 'all';
  shownCap = LIST_CAP;
  fitNext = true;
  writeHash();
  render();
}

function clearFilters(): void {
  F = { ...DEFAULT_FILTERS, c: F.c };
  qEl.value = '';
  shownCap = LIST_CAP; fitNext = true;
  writeHash(); render();
}

// ── Geolocation ──────────────────────────────────────────────────────────────
function requestPosition(radius: number): void {
  if (!('geolocation' in navigator)) { toast(T.geoDenied); return; }
  geoBusy = true; render();
  navigator.geolocation.getCurrentPosition(
    (p) => { pos = { lat: p.coords.latitude, lon: p.coords.longitude }; geoBusy = false; map.setUserLocation(pos); setFilter('d', radius); render(); },
    () => { geoBusy = false; pos = null; toast(T.geoDenied); setFilter('d', 0); render(); },
    { enableHighAccuracy: false, timeout: 15_000, maximumAge: 300_000 },
  );
}

// ── Filter UI events ─────────────────────────────────────────────────────────
function onChip(e: Event): void {
  const b = (e.target as HTMLElement).closest<HTMLElement>('[data-g]');
  if (!b || (b as HTMLButtonElement).disabled) return;
  const g = b.dataset.g as keyof Filters, v = b.dataset.v!;
  switch (g) {
    case 'w': setFilter('w', Number(v)); break;
    case 's': setFilter('s', F.s === v && v === 'active' ? 'all' : (v as Filters['s'])); break;
    case 'k': setFilter('k', F.k === v && v !== 'all' ? 'all' : (v as Filters['k'])); break;
    case 'cat': setFilter('cat', v as Filters['cat']); break;
    case 'r': setFilter('r', v); break;
    case 'b': setFilter('b', !F.b); break;
    case 'd': { const d = Number(v); if (d && !pos) requestPosition(d); else setFilter('d', d); break; }
    default: break;
  }
}
railEl.addEventListener('click', (e) => {
  if ((e.target as HTMLElement).closest('[data-open-filters]')) { openFilters(); return; }
  onChip(e);
});
filterSheet.el.addEventListener('click', (e) => {
  if ((e.target as HTMLElement).closest('[data-clear-filters]')) { clearFilters(); return; }
  onChip(e);
});
function openFilters(): void {
  filterSheet.setContent(panelHTML(F, facetCounts(store.rows.values(), F, { now: Date.now(), pos }), !!pos, geoBusy));
  filterSheet.show(railEl.querySelector<HTMLElement>('[data-open-filters]'));
}
segEl.addEventListener('click', (e) => {
  const b = (e.target as HTMLElement).closest<HTMLButtonElement>('button'); if (!b) return;
  setFilter('c', b.dataset.c as Filters['c']);
});
qEl.addEventListener('input', debounce(() => { F = { ...F, q: qEl.value.trim() }; shownCap = LIST_CAP; writeHash(); render(); }, 150));
document.querySelector('.view')!.addEventListener('click', (e) => {
  const b = (e.target as HTMLElement).closest<HTMLButtonElement>('button'); if (b) setView(b.dataset.v as 'list' | 'map');
});
backBtn.addEventListener('click', () => setView('list'));
statsEl.addEventListener('click', (e) => {
  if ((e.target as HTMLElement).closest('#srcBtn')) openSources();
});
function openSources(): void {
  const now = Date.now();
  sourceSheet.setContent(sourcesHTML(sourceHealth(store.rows.values(), sourceStatus, now), now));
  sourceSheet.show(document.getElementById('srcBtn'));
  if (!FIXTURE_MODE) fetchSourceStatus().then((s) => { if (s) { sourceStatus = s; if (sourceSheet.isOpen()) sourceSheet.setContent(sourcesHTML(sourceHealth(store.rows.values(), s, Date.now()), Date.now())); } });
}

notifyBtn.addEventListener('click', async () => {
  const r = await notifier.toggle();
  toast(r === 'on' ? T.notifyOn : r === 'off' ? T.notifyOff : T.notifyDenied);
  syncNotifyBtn();
});
function syncNotifyBtn(): void {
  notifyBtn.setAttribute('aria-pressed', String(notifier.enabled));
  notifyBtn.hidden = !notifier.supported();
}

window.addEventListener('popstate', () => {
  if (detailSheet.handlePopstate() || filterSheet.handlePopstate() || sourceSheet.handlePopstate()) { writeHash(); return; }
  readHash(); render();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  if (detailSheet.isOpen()) detailSheet.close();
  else if (filterSheet.isOpen()) filterSheet.close();
  else if (sourceSheet.isOpen()) sourceSheet.close();
});
bk.addEventListener('click', () => { detailSheet.close(); filterSheet.close(); sourceSheet.close(); });
let lastPhone = isPhone();
window.addEventListener('resize', debounce(() => {
  const p = isPhone();
  if (p !== lastPhone) { lastPhone = p; setView(p ? view : 'list'); }
  map.invalidate(); if (!p) map.reveal();
}, 150));

// ── Data flow ────────────────────────────────────────────────────────────────
function onChanged(cs: ChangeSet): void {
  if (firstLoadDone && cs.added.length) {
    toast(`+${cs.added.length} novih · skupaj ${store.size}`);
    notifier.notifyNew(cs.added.map((id) => store.rows.get(id)!).filter(Boolean), Date.now());
  }
  if (selected) { const i = store.rows.get(selected); if (i) renderDetail(i); else detailSheet.close(); }
  render();
}
store.changed.on(onChanged);

async function loadFull(): Promise<ChangeSet> {
  if (inflight) return inflight;
  inflight = (async () => {
    const { rows, capped } = await fetchInitial();
    if (capped.length) console.warn(`LIST_CAP reached for ${capped.join(',')} – oldest rows are not shown`);
    const cs = store.replaceAll(rows);
    lastSync = Date.now();
    if (rtStatus !== 'live') rtStatus = 'polling';
    syncHeader();
    return cs;
  })().catch((e) => { markOffline(e); throw e; }).finally(() => { inflight = null; });
  return inflight;
}

async function loadIncremental(): Promise<ChangeSet> {
  if (!store.maxSeen || FIXTURE_MODE) return loadFull();
  if (inflight) return inflight;
  inflight = (async () => {
    const rows = await fetchIncremental(store.maxSeen);
    const cs = store.upsert(rows);
    lastSync = Date.now();
    if (rtStatus === 'offline') rtStatus = 'polling';
    syncHeader();
    return cs;
  })().catch((e) => { markOffline(e); throw e; }).finally(() => { inflight = null; });
  return inflight;
}

function markOffline(e: unknown): void {
  rtStatus = 'offline'; syncHeader();
  if (!sbDownToasted) { sbDownToasted = true; toast(T.sbDown); window.setTimeout(() => { sbDownToasted = false; }, 120_000); }
  console.warn('load failed', e);
}

/** One tiny request: has the poller finished a cycle since we last looked? */
async function headCheck(): Promise<void> {
  if (FIXTURE_MODE || document.visibilityState === 'hidden') return;
  try {
    control = await readControl();
    const fin = control?.poll_finished_at || '';
    if (fin && fin !== lastControlFinished) { lastControlFinished = fin; await loadIncremental().catch(() => undefined); }
    else if (rtStatus === 'offline') { rtStatus = 'polling'; }
    syncHeader();
  } catch (e) { markOffline(e); }
}

// Realtime: coalesce a burst of postgres_changes into ONE incremental fetch.
let rtPending: string[] = [], rtDeletes: string[] = [], rtTimer = 0;
function rtFlush(): void {
  rtTimer = 0;
  if (rtDeletes.length) { store.remove(rtDeletes); rtDeletes = []; }
  if (rtPending.length) { rtPending = []; loadIncremental().catch(() => undefined); }
}
async function startRealtime(): Promise<void> {
  if (FIXTURE_MODE) return;
  try {
    const { createClient } = await import('@supabase/supabase-js');
    const client = createClient(SB_URL, SB_KEY, {
      auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false },
      realtime: { params: { eventsPerSecond: 5 } },
    });
    client.channel('vatrocad-incidents')
      .on('postgres_changes', { event: '*', schema: 'public', table: 'incidents' }, (payload) => {
        const rec = (payload.eventType === 'DELETE' ? payload.old : payload.new) as { id?: string } | undefined;
        if (payload.eventType === 'DELETE') { if (rec?.id) rtDeletes.push(rec.id); }
        else if (rec?.id) rtPending.push(rec.id);
        if (!rtTimer) rtTimer = window.setTimeout(rtFlush, RT_DEBOUNCE_MS);
      })
      .subscribe((status) => {
        if (status === 'SUBSCRIBED') rtStatus = 'live';
        else if (rtStatus === 'live') rtStatus = 'polling';
        syncHeader();
      });
  } catch (e) { console.warn('realtime unavailable', e); rtStatus = rtStatus === 'offline' ? 'offline' : 'polling'; syncHeader(); }
}

// Osveži: reload → ask the poller → wait for control.poll_finished_at → reload.
refreshBtn.addEventListener('click', async () => {
  if (refreshing) return;
  const now = Date.now();
  if (now - lastRefreshAt < REFRESH_THROTTLE_MS) { toast(T.throttled); return; }
  lastRefreshAt = now; refreshing = true;
  refreshBtn.disabled = true; refreshBtn.classList.add('spin'); refreshBtn.setAttribute('aria-busy', 'true');
  try {
    const first = await (FIXTURE_MODE ? loadFull() : loadIncremental());
    const before = await readControl().catch(() => null);
    control = before;
    const n1 = first.added.length;
    if (FIXTURE_MODE) { toast(n1 ? `+${n1} novih · skupaj ${store.size}` : T.pollerIdle); return; }
    if (!before) { toast(T.sbDown); return; }
    const asked = await requestPoll().catch(() => false);
    if (!asked || !runnerUp(before)) { toast(n1 ? `+${n1} novih · skupaj ${store.size}` : T.pollerIdle); return; }
    toast(n1 ? `+${n1} novih · ${T.fetching}` : T.fetching, 6000);
    const prevFin = before.poll_finished_at || '';
    const t0 = Date.now();
    while (Date.now() - t0 < POLL_WAIT_MS) {
      await new Promise((r) => setTimeout(r, POLL_STEP_MS));
      const c = await readControl().catch(() => null);
      if (c) { control = c; syncHeader(); }
      if (c && (c.poll_finished_at || '') > prevFin) {
        lastControlFinished = c.poll_finished_at || '';
        const res = await loadIncremental();
        toast(res.added.length ? `+${res.added.length} novih · skupaj ${store.size}` : T.fresh);
        return;
      }
    }
    toast(T.pollerTimeout);
  } catch { toast(T.sbDown); }
  finally { refreshing = false; refreshBtn.disabled = false; refreshBtn.classList.remove('spin'); refreshBtn.removeAttribute('aria-busy'); }
});

document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') { headCheck(); renderNow(); } });
window.addEventListener('online', () => headCheck());

// ── Boot ─────────────────────────────────────────────────────────────────────
async function boot(): Promise<void> {
  readHash();
  if (isPhone()) setView(view); else setView('list');
  syncNotifyBtn();
  tickClock(); window.setInterval(tickClock, 1000);
  listEl.innerHTML = `<div class="empty" role="status">${ic('refresh')} Nalagam…</div>`;
  segEl.innerHTML = (['all', 'AT', 'HR'] as const).map((c) =>
    `<button type="button" data-c="${c}" aria-pressed="${F.c === c}">${esc(COUNTRY_LABEL[c] ?? c)} <span class="n">0</span></button>`).join('');
  try { await loadFull(); } catch { /* header already shows offline */ }
  firstLoadDone = true;
  render();
  if (!FIXTURE_MODE) {
    headCheck();
    startRealtime();
    window.setInterval(headCheck, HEAD_CHECK_MS);
    window.setInterval(() => { if (document.visibilityState === 'visible') loadFull().catch(() => undefined); }, FULL_RELOAD_MS);
  } else {
    rtStatus = 'polling'; syncHeader();
  }
  window.setInterval(() => { list.tick((id) => store.rows.get(id), Date.now()); syncHeader(); render(); }, TICK_MS);
  if (!isPhone()) map.reveal();
  if (import.meta.env.PROD && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register(`${BASE}sw.js`, { scope: BASE }).catch(() => undefined);
  }
}

boot();
