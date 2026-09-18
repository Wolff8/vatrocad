import type { Category, Country, Incident, Kind, RawIncident, Status } from './types';
import { AT_KIND, AT_LEVEL, CAT, STATUS, STATUS_LABEL_REPORT, regionLabel } from './labels';
import { localToEpoch, parseIso } from './time';
import { isFiniteCoord } from './geo';

const CATS = new Set<string>(Object.keys(CAT));
const STATUSES = new Set<string>(['active', 'contained', 'closed']);

export const fold = (s: string | null | undefined): string =>
  String(s ?? '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');

export const isPolice = (i: Pick<RawIncident, 'source'>): boolean =>
  /^(Policija|PU |MUP)/.test(i.source || '');

/** AT rows that are after-action reports rather than dispatch-log lines. */
export const isAtReport = (i: Pick<RawIncident, 'country' | 'ref'>): boolean =>
  i.country === 'AT' && !/^(NOE|OOE)-/.test(i.ref || '');

const HR_NEWSROOM = /· (kronika|vijesti)$|^HVZ ·/;

export function kindOf(i: Pick<RawIncident, 'source' | 'country' | 'ref' | 'category'>): Kind {
  if (i.category === 'weather') return 'weather';
  if (isPolice(i)) return 'police';
  if (i.category === 'summary') return 'report';
  if (isAtReport(i)) return 'report';
  if (i.country !== 'AT' && HR_NEWSROOM.test(i.source || '')) return 'report';
  return 'dispatch';
}

/** Air rescue: Christophorus / C1x / RK-1,2 / Notarzthubschrauber / HGSS helicopter. */
export const isHeli = (text: string | null | undefined): boolean =>
  /hubschrauber|christophorus|\bC\s?1\d\b|\bRK-?[12]\b|helikopter|landeplatz|flugrettung/i.test(text || '');
/** Ground EMS and rescue services (DE + HR + SL vocabulary). */
export const isEms = (text: string | null | undefined): boolean =>
  /notarzt|notärzt|rettungsdienst|rotes kreuz|\brettung\b|rettungswagen|sanitäter|samariter|\bRTW\b|\bNEF\b|hitna pomoć|hitne pomoći|hitnu pomoć|\bhitna\b|\bZHM\b|reševalc|\bNMP\b|zdravnik/i.test(text || '');

export function normCategory(c: string | null | undefined): Category {
  return c && CATS.has(c) ? (c as Category) : 'other';
}
export function normStatus(s: string | null | undefined): Status {
  return s && STATUSES.has(s) ? (s as Status) : 'unknown';
}

/** One definition of "active" for both countries: active or contained, never an exercise. */
export function isActive(cat: Category, st: Status): boolean {
  return (st === 'active' || st === 'contained') && cat !== 'exercise';
}

export function statusLabel(i: Pick<Incident, 'kind' | 'st' | 'cat'>): string {
  if (i.st === 'active' || i.st === 'contained') return STATUS[i.st];
  if (i.kind === 'report' || i.kind === 'police') return STATUS_LABEL_REPORT;
  if (i.cat === 'weather') return '';
  return STATUS[i.st] || '';
}
export function statusClass(i: Pick<Incident, 'kind' | 'st' | 'cat'>): string {
  if (i.st === 'active' || i.st === 'contained') return i.st;
  if (i.kind === 'report' || i.kind === 'police') return 'report';
  return i.st;
}

/** Enrich a PostgREST row once; everything derived lives on the object. */
export function mapRow(r: RawIncident, prev?: Incident): Incident {
  const country: Country = r.country === 'AT' ? 'AT' : 'HR';
  const cat = normCategory(r.category);
  const st = normStatus(r.status);
  const kind = kindOf({ source: r.source, country, ref: r.ref, category: r.category });
  const epoch = localToEpoch(r.occurred, r.occurred_time) ?? parseIso(r.ts);
  const firstSeenMs = parseIso(r.first_seen) ?? parseIso(r.last_seen) ?? Date.now();
  const lastSeenMs = parseIso(r.last_seen) ?? firstSeenMs;
  const lat = isFiniteCoord(r.lat, r.lon) ? r.lat : null;
  const lon = lat === null ? null : (r.lon as number);
  const hasRaw = r.raw !== undefined || (prev?.hasRaw ?? false);
  const raw = r.raw !== undefined ? r.raw : prev?.raw;
  const raw_sl = r.raw_sl !== undefined ? r.raw_sl : prev?.raw_sl;
  const hay = fold([
    r.title_sl, r.title, r.location, r.units, r.source, r.ref,
    CAT[cat].label, regionLabel(country, r.region),
  ].join(' '));
  return {
    ...r, country, cat, st, kind, epoch, sortMs: epoch ?? firstSeenMs,
    firstSeenMs, lastSeenMs, lat, lon,
    active: isActive(cat, st),
    heli: isHeli(r.title) || isHeli(r.units) || isHeli(raw),
    ems: isEms(r.title) || isEms(r.units) || isEms(raw),
    hay, hasRaw, raw, raw_sl,
  };
}

export function iconOf(i: Pick<Incident, 'cat' | 'kind' | 'heli'>): string {
  if (i.heli && (i.cat === 'ems' || i.cat === 'rescue' || i.cat === 'accident')) return 'heli';
  if (i.kind === 'police' && (i.cat === 'other' || i.cat === 'summary')) return 'police';
  return CAT[i.cat].icon;
}
export const colorOf = (i: Pick<Incident, 'cat'>): string => CAT[i.cat].color;

// ── Slovenian gloss for the fixed dispatch vocabulary (fallback when the
// poller has not translated a row yet). Longest phrases first; unmatched
// words stay in the source language rather than being guessed at.
const DE_SL: [string, string][] = [
  ['Brandsicherheitswache', 'požarna straža'], ['Fahrzeugbrand - PKW', 'požar osebnega vozila'],
  ['Fahrzeugbrand - LKW', 'požar tovornega vozila'], ['Fahrzeugbrand', 'požar vozila'],
  ['Gefahrenmeldeanlage - Brand', 'sprožen požarni javljalnik'], ['Kleinbrand - im Freien', 'manjši požar na prostem'],
  ['Kleinbrand', 'manjši požar'], ['Rauchentwicklung', 'razvoj dima'], ['Vegetationsbrand', 'požar vegetacije'],
  ['Gebäudebrand - Landwirtschaft', 'požar kmetijske zgradbe'], ['Gebäudebrand - Wohnhaus', 'požar stanovanjske hiše'],
  ['Gebäudebrand', 'požar zgradbe'], ['Wohnhausbrand', 'požar stanovanjske hiše'], ['Wohnungsbrand', 'požar stanovanja'],
  ['Zimmerbrand', 'požar sobe'], ['Küchenbrand', 'požar v kuhinji'], ['Waldbrand', 'gozdni požar'],
  ['Kaminbrand', 'požar dimnika'], ['Flurbrand', 'požar travnika'], ['Müllbrand', 'požar odpadkov'],
  ['Großbrand', 'večji požar'], ['Brandverdacht', 'sum požara'], ['Brandmeldealarm', 'alarm požarnega javljalnika'],
  ['Verkehrsunfall mit eingeklemmter Person', 'prometna nesreča z ukleščeno osebo'],
  ['Verkehrsunfall - Verletzungen', 'prometna nesreča s poškodovanimi'],
  ['Verkehrsunfall Aufräumarbeiten', 'čiščenje po prometni nesreči'], ['Verkehrsunfall', 'prometna nesreča'],
  ['Menschenrettung', 'reševanje osebe'], ['Personenrettung', 'reševanje oseb'], ['Personensuche', 'iskanje pogrešane osebe'],
  ['Tierrettung', 'reševanje živali'], ['Türöffnung', 'odpiranje vrat'], ['Notöffnung', 'nujno odpiranje'],
  ['Bergung - PKW', 'izvlek osebnega vozila'], ['Bergung - LKW', 'izvlek tovornega vozila'], ['Bergung', 'izvlek'],
  ['Technische Hilfeleistung', 'tehnična pomoč'], ['Auspumparbeiten', 'izčrpavanje vode'],
  ['Wasserschaden', 'škoda zaradi vode'], ['Sturmschaden', 'škoda zaradi neurja'], ['Unwetter', 'neurje'],
  ['Hagelunwetter', 'neurje s točo'], ['Ölspur', 'sled olja'], ['Ölaustritt', 'iztekanje olja'],
  ['Gasaustritt', 'uhajanje plina'], ['Gasgeruch', 'vonj po plinu'], ['Evakuierung', 'evakuacija'],
  ['Einsatzübung', 'vaja'], ['Übung', 'vaja'], ['Rettungshubschrauber', 'reševalni helikopter'],
  ['Hubschrauber', 'helikopter'], ['Rettungsdienst', 'reševalna služba'], ['Notarzt', 'zdravnik NMP'],
  ['Wirtschaftsgebäude', 'gospodarsko poslopje'], ['Wohnhaus', 'stanovanjska hiša'], ['Gebäude', 'zgradba'],
  ['Landwirtschaftlicher Betrieb', 'kmetija'], ['Gartenhütte', 'vrtna lopa'], ['Motorrad', 'motorno kolo'],
  ['Radfahrer', 'kolesar'], ['Fußgänger', 'pešec'], ['verletzt', 'poškodovan'], ['gerettet', 'rešen'],
  ['abgebrannt', 'pogorela'], ['in Brand', 'v ognju'], ['Brand', 'požar'], ['Unfall', 'nesreča'],
  ['im Freien', 'na prostem'], ['PKW', 'osebno vozilo'], ['LKW', 'tovornjak'],
];
const HR_SL: [string, string][] = [
  ['prometna nesreća', 'prometna nesreča'], ['prometnoj nesreći', 'prometni nesreči'], ['prometne nesreće', 'prometne nesreče'],
  ['požar otvorenog prostora', 'požar odprtega prostora'], ['požar na otvorenom', 'požar na prostem'],
  ['tehnička intervencija', 'tehnična intervencija'], ['spašavanja ljudi', 'reševanja ljudi'], ['spašavanje', 'reševanje'],
  ['teško ozlijeđen', 'hudo poškodovan'], ['ozlijeđen', 'poškodovan'], ['ozlijeđeni', 'poškodovani'],
  ['poginuo', 'umrl'], ['smrtno stradao', 'umrl'], ['vatrogasci', 'gasilci'], ['vatrogasac', 'gasilec'],
  ['vozilo', 'vozilo'], ['kuće', 'hiše'], ['kuća', 'hiša'], ['gori', 'gori'], ['ugašen', 'pogašen'],
  ['lokaliziran', 'lokaliziran'], ['pronađeno tijelo', 'najdeno truplo'], ['na području', 'na območju'],
  ['dojava zaprimljena', 'dojava prejeta'], ['dojava je zaprimljena', 'dojava je prejeta'],
];

function gloss(text: string, table: [string, string][]): string {
  let out = text;
  for (const [src, dst] of table) {
    const pat = src.length > 7 ? escapeRe(src) : `\\b${escapeRe(src)}\\b`;
    out = out.replace(new RegExp(pat, 'gi'), (m) => (m[0] === m[0]?.toUpperCase() && m[0] !== m[0]?.toLowerCase() ? cap(dst) : dst));
  }
  return out;
}
const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const cap = (s: string) => (s ? s[0]!.toUpperCase() + s.slice(1) : s);

// Meteoalarm titles are English ('Yellow wind warning'); render them in Slovenian.
const WX_RE = /^(yellow|orange|red)\s+(.+?)\s+warning$/i;
const WX_LEVEL: Record<string, string> = { yellow: 'Rumeno', orange: 'Oranžno', red: 'Rdeče' };
const WX_TYPE: Record<string, string> = {
  wind: 'veter', thunderstorm: 'nevihte', thunderstorms: 'nevihte', rain: 'dež', 'rain-flood': 'dež in poplave',
  'snow-ice': 'sneg in led', 'snow/ice': 'sneg in led', snow: 'sneg', ice: 'led', heat: 'vročina', 'high temperature': 'vročina',
  'low temperature': 'mraz', cold: 'mraz', fog: 'megla', 'coastal event': 'obalni pojav', coastalevent: 'obalni pojav',
  'forest fire': 'požarna ogroženost', forestfire: 'požarna ogroženost', flood: 'poplave', flooding: 'poplave',
  avalanche: 'snežni plazovi', avalanches: 'snežni plazovi',
};
export function weatherTitle(title: string): string | null {
  const m = WX_RE.exec(tidy(title));
  if (!m) return null;
  const lvl = WX_LEVEL[m[1]!.toLowerCase()] ?? cap(m[1]!);
  const type = WX_TYPE[m[2]!.toLowerCase()] ?? m[2]!;
  return `${lvl} opozorilo: ${type}`;
}

/** Best display title: Slovenian when the poller translated it, else a glossed original. */
export function displayTitle(i: Pick<Incident, 'title' | 'title_sl' | 'country' | 'kind'>): string {
  if (i.kind === 'weather') { const w = weatherTitle(i.title || ''); if (w) return w; }
  if (i.title_sl && i.title_sl.trim()) return tidy(i.title_sl);
  const t = tidy(i.title || '');
  if (!t) return '';
  return i.country === 'AT' ? gloss(t, DE_SL) : gloss(t, HR_SL);
}
export const isTranslated = (i: Pick<Incident, 'title' | 'title_sl' | 'raw' | 'raw_sl'>): boolean =>
  !!(i.title_sl && i.title_sl !== i.title) || !!(i.raw_sl && i.raw_sl !== i.raw);

export function tidy(s: string): string {
  return s.replace(/[​‌﻿]/g, '').replace(/\s+/g, ' ').trim();
}

// Alarm codes are one digit ("B1", "T2 -", "SOF2"); the (?!\d) guard keeps a
// road number out of it — "B83 Krumpendorf" was rendering as "3 Krumpendorf".
const AT_CODE = /^(SOF|[BTSU])\s?\d(?!\d)\s*[-–:]?\s*/i;
const RUNNING = /\s*\((v teku|läuft)[^)]*\)\s*$/i;

/** Subject of the incident: the title stripped of dispatch codes / running-time suffixes,
 *  trimmed at a word boundary. */
export function subjectOf(i: Pick<Incident, 'title' | 'title_sl' | 'country' | 'kind' | 'cat'>, max = 72): string {
  let s = displayTitle(i).replace(AT_CODE, '').replace(RUNNING, '').trim();
  s = s.replace(/^[-–·:\s]+/, '');
  if (!s) s = CAT[i.cat].label;
  s = cap(s);
  if (s.length > max) {
    const cut = s.slice(0, max - 1);
    const sp = cut.lastIndexOf(' ');
    s = (sp > max * 0.6 ? cut.slice(0, sp) : cut).replace(/[\s,;:–-]+$/, '') + '…';
  }
  return s;
}

/** Short place: the settlement without a trailing "(district)" or region qualifier. */
export function shortPlace(location: string | null | undefined): string {
  const l = tidy(location || '');
  if (!l) return '';
  return l.replace(/\s*\([^)]*\)\s*$/, '').replace(/\s*,\s*[^,]+$/, (m) => (l.length > 40 ? '' : m)).trim();
}

/** Dispatch-style headline: 'POŽAR · Stanovanjski objekt · Leibnitz'. Parts are
 *  returned separately so the renderer can style them. */
export function dispatchTitle(i: Pick<Incident, 'title' | 'title_sl' | 'country' | 'kind' | 'cat' | 'location'>): { head: string; subject: string; place: string } {
  const head = CAT[i.cat].head;
  const subject = subjectOf(i);
  const place = shortPlace(i.location);
  return { head, subject, place };
}

/** Explains the AT alarm code (B3, T1, SOF2 …) or the row's provenance. */
export function atExplain(i: Pick<Incident, 'title' | 'raw' | 'kind' | 'country' | 'region'>): string {
  const m = /^(SOF|[BTSU])(\d)\b/.exec(i.raw || i.title || '');
  if (m) return `${AT_KIND[m[1]!] || m[1]} · ${AT_LEVEL[m[2]!] || 'stopnja ' + m[2]}`;
  if (i.kind === 'police') return 'Uradno sporočilo policijske uprave, objavljeno naknadno. Datum in ura dogodka sta iz besedila sporočila, sicer čas objave.';
  if (i.kind === 'report') return 'Poročilo enote ali kronika, objavljeno naknadno — brez dispečerske kode. Datum in ura sta čas dogodka iz besedila, sicer čas objave.';
  if (i.country === 'AT' && i.region === 'ooe') return 'Opisni naziv brez alarmne kode (Zgornja Avstrija kod ne uporablja).';
  return '';
}

/** Split a comma/semicolon separated unit list into chips. */
export function unitChips(units: string | null | undefined, max = 6): { chips: string[]; more: number } {
  const u = tidy(units || '');
  if (!u || u === '—') return { chips: [], more: 0 };
  const parts = u.split(/\s*[,;]\s*|\s+·\s+/).map((p) => p.trim()).filter(Boolean);
  const uniq = [...new Set(parts)];
  return { chips: uniq.slice(0, max), more: Math.max(0, uniq.length - max) };
}

/** Only http(s) links are ever rendered as anchors (scraped links are untrusted). */
export function safeLink(link: string | null | undefined): string | null {
  const l = (link || '').trim();
  return /^https?:\/\/[^\s"'<>]+$/i.test(l) ? l : null;
}

/** Narrative pieces for the detail view. */
export function narrativeOf(i: Pick<Incident, 'raw' | 'raw_sl' | 'country' | 'title' | 'title_sl'>): { sl: string | null; orig: string | null } {
  const orig = i.raw ? tidy(i.raw) : null;
  let sl = i.raw_sl && i.raw_sl !== i.raw ? tidy(i.raw_sl) : null;
  if (!sl && orig && i.country === 'AT' && orig.length < 200) sl = gloss(orig, DE_SL);
  if (sl && orig && sl === orig) sl = null;
  return { sl, orig };
}

/** True for rows first seen in the last `minutes`. */
export const isNew = (i: Pick<Incident, 'firstSeenMs'>, now: number, minutes: number): boolean =>
  now - i.firstSeenMs <= minutes * 60_000 && i.firstSeenMs <= now + 60_000;
