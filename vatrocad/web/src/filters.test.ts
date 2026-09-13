import { describe, expect, it } from 'vitest';
import type { RawIncident } from './types';
import { mapRow, dispatchTitle, kindOf, statusLabel, safeLink, subjectOf, unitChips, isNew, weatherTitle } from './classify';
import { DEFAULT_FILTERS, applyFilters, facetCounts, groupByBand, hourHistogram, parseHash, sortIncidents, toHash, type Filters } from './filters';
import { rebaseFixture } from './data';
import { localToEpoch } from './time';

const NOW = Date.UTC(2026, 8, 10, 12, 0); // 14:00 CEST

function raw(p: Partial<RawIncident> & { id: string }): RawIncident {
  return {
    source: 'NÖ Feuerwehr · Wastl', region: 'noe', country: 'AT', ref: 'NOE-0910-1200', occurred: '2026-09-10', occurred_time: '13:30',
    ts: null, category: 'fire', status: 'closed', title: 'B2 Gebäudebrand - Wohnhaus', location: 'Leibnitz (Steiermark)', lat: 46.78, lon: 15.54,
    units: 'Feuerwehr Leibnitz, Feuerwehr Kaindorf', crew: null, vehicles: null, link: 'https://example.org/x', title_sl: null,
    first_seen: '2026-09-10T11:35:00+00:00', last_seen: '2026-09-10T11:50:00+00:00', ...p,
  };
}

const rows = [
  mapRow(raw({ id: 'a1', status: 'active', occurred_time: '13:40' })),
  mapRow(raw({ id: 'c1', status: 'closed', occurred_time: '13:50' })),
  mapRow(raw({ id: 'h1', country: 'HR', region: 'sib', source: 'JVP Šibenik', ref: 'ŠI-0910', category: 'accident', status: 'unknown', occurred_time: '09:00', title: 'Prometna nesreća kod Šibenika', title_sl: 'Prometna nesreča pri Šibeniku', location: 'Šibenik', units: 'JVP Šibenik', lat: 43.73, lon: 15.89, first_seen: '2026-09-10T07:10:00+00:00' })),
  mapRow(raw({ id: 'p1', source: 'Policija Štajerska · LPD', ref: 'POL-STMK-0909', region: 'stmk', occurred: '2026-09-09', occurred_time: '16:00', category: 'accident', title: 'Verkehrsunfall zwischen Pkw und Motorrad', location: 'Graz', units: 'Polizei', first_seen: '2026-09-09T15:00:00+00:00' })),
  mapRow(raw({ id: 'x1', category: 'exercise', title: 'U1 Übung', occurred_time: '13:55' })),
  mapRow(raw({ id: 'n1', occurred: null, occurred_time: null, lat: null, lon: null, first_seen: '2026-09-04T10:00:00+00:00' })),
  mapRow(raw({ id: 'w1', country: 'HR', region: 'nat', source: 'Meteoalarm', category: 'weather', status: 'unknown', occurred: '2026-09-10', occurred_time: '18:00', title: 'Yellow thunderstorm warning', location: 'Gospić region', units: 'Moderate', lat: null, lon: null })),
  mapRow(raw({ id: 'old', occurred: '2026-09-05', occurred_time: '10:00' })),
];
const byId = (id: string) => rows.find((r) => r.id === id)!;

describe('classification', () => {
  it('kinds', () => {
    expect(kindOf(byId('a1'))).toBe('dispatch');
    expect(kindOf(byId('p1'))).toBe('police');
    expect(kindOf(byId('w1'))).toBe('weather');
    expect(kindOf({ source: 'LFV Štajerska · poročila', country: 'AT', ref: 'STMK-LFV-0910', category: 'fire' })).toBe('report');
    expect(kindOf({ source: 'Index · vijesti', country: 'HR', ref: 'IDX-1', category: 'fire' })).toBe('report');
    expect(kindOf({ source: 'PU istarska', country: 'HR', ref: 'PU-1', category: 'accident' })).toBe('police');
  });
  it('active definition and status labels', () => {
    expect(byId('a1').active).toBe(true);
    expect(byId('x1').active).toBe(false);
    expect(statusLabel(byId('a1'))).toBe('AKTIVNO');
    expect(statusLabel(byId('c1'))).toBe('ZAKLJUČENO');
    expect(statusLabel(byId('p1'))).toBe('POROČILO');
    expect(statusLabel(byId('h1'))).toBe('');
  });
  it('dispatch title strips codes, glosses German, keeps place short', () => {
    expect(dispatchTitle(byId('a1'))).toEqual({ head: 'POŽAR', subject: 'Požar stanovanjske hiše', place: 'Leibnitz' });
    expect(dispatchTitle(byId('h1')).subject).toBe('Prometna nesreča pri Šibeniku');
    expect(dispatchTitle(byId('p1')).subject).toBe('Prometna nesreča zwischen Osebno vozilo und Motorno kolo');
    expect(subjectOf({ title: 'T1 Bergung - PKW (v teku ~1 h)', title_sl: null, country: 'AT', kind: 'dispatch', cat: 'tech' })).toBe('Izvlek osebnega vozila');
    expect(dispatchTitle(byId('w1')).subject).toBe('Rumeno opozorilo: nevihte');
    expect(weatherTitle('Orange coastal event warning')).toBe('Oranžno opozorilo: obalni pojav');
    expect(weatherTitle('Požar')).toBeNull();
  });
  it('null occurred does not crash; epoch falls back sensibly', () => {
    expect(byId('n1').epoch).toBeNull();
    expect(byId('n1').sortMs).toBe(Date.parse('2026-09-04T10:00:00+00:00'));
    expect(byId('n1').lat).toBeNull();
  });
  it('unit chips and safe links', () => {
    expect(unitChips('Feuerwehr A, Feuerwehr B, Feuerwehr C', 2)).toEqual({ chips: ['Feuerwehr A', 'Feuerwehr B'], more: 1 });
    expect(unitChips('—')).toEqual({ chips: [], more: 0 });
    expect(safeLink('https://ok.example/x?y=1')).toBe('https://ok.example/x?y=1');
    expect(safeLink('javascript:alert(1)')).toBeNull();
    expect(safeLink('ftp://x')).toBeNull();
  });
  it('NOVO within 30 minutes of first_seen', () => {
    expect(isNew(byId('a1'), NOW, 30)).toBe(true);
    expect(isNew(byId('h1'), NOW, 30)).toBe(false);
  });
});

describe('sorting and grouping', () => {
  it('active first, then newest', () => {
    const s = sortIncidents(rows).map((r) => r.id);
    expect(s[0]).toBe('a1');
    expect(s.indexOf('c1')).toBeLessThan(s.indexOf('h1'));
    expect(s.indexOf('x1')).toBe(s.length - 1); // exercises last
    expect(s.indexOf('w1')).toBe(s.length - 2); // weather just before
    expect(s.indexOf('n1')).toBeGreaterThan(s.indexOf('old')); // unknown time sorts by first_seen
  });
  it('bands: active rows are pulled into "Zadnja ura"', () => {
    const g = groupByBand(sortIncidents(rows), NOW);
    expect(g.map((x) => x.key)).toEqual(['hour', 'today', 'yesterday', 'older', 'weather']);
    expect(g.find((x) => x.key === 'hour')!.items.map((i) => i.id)).toEqual(['a1', 'c1', 'x1']);
    expect(g.find((x) => x.key === 'weather')!.items[0]!.id).toBe('w1');
  });
});

describe('filters', () => {
  const f = (p: Partial<Filters> = {}): Filters => ({ ...DEFAULT_FILTERS, ...p });
  it('default 24 h window keeps future weather and drops old rows', () => {
    const ids = applyFilters(rows, f(), { now: NOW }).map((r) => r.id);
    expect(ids).toContain('w1');
    expect(ids).not.toContain('old');
    expect(ids).not.toContain('n1');
    expect(ids).toContain('p1');
  });
  it('7 d window includes rows without a time (via first_seen)', () => {
    expect(applyFilters(rows, f({ w: 168 }), { now: NOW }).map((r) => r.id)).toContain('n1');
  });
  it('country / region / kind / category / status', () => {
    expect(applyFilters(rows, f({ c: 'HR' }), { now: NOW }).map((r) => r.id).sort()).toEqual(['h1', 'w1']);
    expect(applyFilters(rows, f({ r: 'stmk' }), { now: NOW }).map((r) => r.id)).toEqual(['p1']);
    expect(applyFilters(rows, f({ k: 'police' }), { now: NOW }).map((r) => r.id)).toEqual(['p1']);
    expect(applyFilters(rows, f({ cat: 'exercise' }), { now: NOW }).map((r) => r.id)).toEqual(['x1']);
    expect(applyFilters(rows, f({ s: 'active' }), { now: NOW }).map((r) => r.id)).toEqual(['a1']);
    expect(applyFilters(rows, f({ s: 'report' }), { now: NOW }).map((r) => r.id)).toEqual(['p1']);
  });
  it('border belt and text search (diacritics folded)', () => {
    expect(applyFilters(rows, f({ b: true }), { now: NOW }).map((r) => r.id).sort()).toEqual(['h1', 'p1', 'w1']);
    expect(applyFilters(rows, f({ q: 'sibenik' }), { now: NOW }).map((r) => r.id)).toEqual(['h1']);
    expect(applyFilters(rows, f({ q: 'LEIBNITZ' }), { now: NOW }).length).toBe(3);
  });
  it('near-me radius uses haversine and skips rows without coordinates', () => {
    const near = applyFilters(rows, f({ d: 25, w: 168 }), { now: NOW, pos: { lat: 46.8, lon: 15.5 } }).map((r) => r.id);
    expect(near).toContain('a1');
    expect(near).not.toContain('h1');
    expect(near).not.toContain('n1');
  });
  it('facet counts answer "what if I clicked this chip"', () => {
    const c = facetCounts(rows, f({ c: 'AT' }), { now: NOW });
    expect(c.c['HR']).toBe(2);
    expect(c.k['police']).toBe(1);
    expect(c.s['active']).toBe(1);
    expect(c.w['168']).toBeGreaterThan(c.w['1']!);
  });
  it('hour histogram buckets', () => {
    const h = hourHistogram(rows, NOW);
    expect(h[0]).toBe(3); // a1 c1 x1 within the last hour
    expect(h[5]).toBe(1); // h1 at 09:00
    expect(h[22]).toBe(1); // p1 yesterday 16:00
    expect(h.reduce((a, b) => a + b, 0)).toBe(5);
  });
});

describe('URL hash round-trip', () => {
  it('serialises only non-defaults and parses back', () => {
    const f: Filters = { ...DEFAULT_FILTERS, c: 'AT', r: 'stmk', k: 'police', w: 72, q: 'graz', b: true };
    const h = toHash(f, { id: 'POL-1', v: 'map' });
    expect(h).toBe('#c=AT&r=stmk&k=police&w=72&q=graz&b=1&v=map&id=POL-1');
    const p = parseHash(h);
    expect(p.f).toEqual({ c: 'AT', r: 'stmk', k: 'police', w: 72, q: 'graz', b: true });
    expect(p.id).toBe('POL-1');
    expect(p.v).toBe('map');
    expect(toHash(DEFAULT_FILTERS)).toBe('');
  });
  it('ignores garbage', () => {
    const p = parseHash('#c=XX&w=99&k=nope&cat=zz&s=bad&d=7');
    expect(p.f).toEqual({});
  });
});

describe('fixture re-basing', () => {
  it('shifts the newest non-weather row to 12 minutes before now, preserving local wall clock semantics', () => {
    const shifted = rebaseFixture([raw({ id: 'z', occurred: '2026-01-15', occurred_time: '12:00' })], NOW);
    expect(localToEpoch(shifted[0]!.occurred, shifted[0]!.occurred_time)).toBe(NOW - 12 * 60_000);
    expect(shifted[0]!.occurred).toBe('2026-09-10');
  });
});
