import type { FacetCounts, Filters } from '../filters';
import { activeFilterCount } from '../filters';
import { AT_STATE, CAT, CATEGORIES, HR_REG, KIND, KINDS, RADII, T, WINDOWS } from '../labels';
import { esc } from './dom';
import { ic } from './sprite';

const STATUS_CHIPS: { v: Filters['s']; label: string }[] = [
  { v: 'all', label: 'Vse' }, { v: 'active', label: 'Aktivno' }, { v: 'contained', label: 'Lokalizirano' },
  { v: 'closed', label: 'Zaključeno' }, { v: 'report', label: 'Poročila' },
];

const chip = (group: string, value: string, label: string, pressed: boolean, count: number | undefined, icon?: string): string =>
  `<button type="button" class="f" role="radio" aria-checked="${pressed}" data-g="${group}" data-v="${esc(value)}">${icon ? ic(icon) : ''}<span>${esc(label)}</span>${count !== undefined ? `<span class="n">${count}</span>` : ''}</button>`;

/** The quick rail above the list: window + kind + active toggle + Filtri button. */
export function railHTML(f: Filters, c: FacetCounts): string {
  const n = activeFilterCount(f);
  return `
    <button type="button" class="f fbtn ${n ? 'on' : ''}" data-open-filters aria-haspopup="dialog">${ic('filter')}<span>${T.filters}</span>${n ? `<span class="n">${n}</span>` : ''}</button>
    <div class="fg" role="radiogroup" aria-label="Časovno okno">${WINDOWS.map((w) => chip('w', String(w.h), w.label, f.w === w.h, c.w[String(w.h)] ?? 0)).join('')}</div>
    <div class="fg" role="radiogroup" aria-label="Stanje">${chip('s', 'active', 'Aktivno', f.s === 'active', c.s['active'] ?? 0, 'bolt')}</div>
    <div class="fg" role="radiogroup" aria-label="Vrsta vira">${KINDS.map((k) => chip('k', k, KIND[k].label, f.k === k, c.k[k] ?? 0, KIND[k].icon)).join('')}</div>
    <div class="fg" role="radiogroup" aria-label="Reševalci">${chip('t', 'heli', 'Helikopter', f.t === 'heli', c.t['heli'] ?? 0, 'heli')}${chip('t', 'ems', 'Reševalci', f.t === 'ems', c.t['ems'] ?? 0, 'ems')}</div>`;
}

/** Full filter panel (sheet). */
export function panelHTML(f: Filters, c: FacetCounts, hasPos: boolean, geoBusy: boolean): string {
  const regions = f.c === 'AT' ? AT_STATE : f.c === 'HR' ? HR_REG : { ...AT_STATE, ...HR_REG };
  const regionEntries = Object.entries(regions).filter(([k]) => (c.r[k] ?? 0) > 0 || f.r === k);
  const group = (title: string, body: string, g: string) =>
    `<section class="pg"><h3>${esc(title)}</h3><div class="pgc" role="radiogroup" aria-label="${esc(title)}" data-group="${g}">${body}</div></section>`;
  return `
    ${group('Časovno okno', WINDOWS.map((w) => chip('w', String(w.h), w.label, f.w === w.h, c.w[String(w.h)] ?? 0)).join(''), 'w')}
    ${group('Stanje', STATUS_CHIPS.map((s) => chip('s', s.v, s.label, f.s === s.v, c.s[s.v] ?? 0)).join(''), 's')}
    ${group('Vrsta vira', chip('k', 'all', 'Vsi', f.k === 'all', c.k['all'] ?? 0) + KINDS.map((k) => chip('k', k, KIND[k].label, f.k === k, c.k[k] ?? 0, KIND[k].icon)).join(''), 'k')}
    ${group('Kategorija', chip('cat', 'all', 'Vse', f.cat === 'all', c.cat['all'] ?? 0) + CATEGORIES.map((k) => chip('cat', k, CAT[k].short, f.cat === k, c.cat[k] ?? 0, CAT[k].icon)).join(''), 'cat')}
    ${group('Reševalci in helikopter', chip('t', 'all', 'Vse', f.t === 'all', c.t['all'] ?? 0)
      + chip('t', 'heli', 'Reševalni helikopter', f.t === 'heli', c.t['heli'] ?? 0, 'heli')
      + chip('t', 'ems', 'Reševalci / NMP', f.t === 'ems', c.t['ems'] ?? 0, 'ems')
      + '<p class="hint">Dogodki, pri katerih besedilo omenja Christophorus / reševalni helikopter oziroma reševalce, zdravnika NMP ali Rdeči križ.</p>', 't')}
    ${group('Regija', chip('r', 'all', 'Vse regije', f.r === 'all', c.r['all'] ?? 0) + regionEntries.map(([k, l]) => chip('r', k, l, f.r === k, c.r[k] ?? 0)).join(''), 'r')}
    ${group('Avstrija', `<button type="button" class="f" role="switch" aria-checked="${f.b}" data-g="b" data-v="1">${ic('pin')}<span>Samo obmejni pas (Štajerska, Koroška)</span><span class="n">${c.b}</span></button>`, 'b')}
    ${group(T.nearby, `<button type="button" class="f" role="radio" aria-checked="${f.d === 0}" data-g="d" data-v="0">${ic('near')}<span>Izklopljeno</span></button>` +
      RADII.map((r) => `<button type="button" class="f" role="radio" aria-checked="${f.d === r}" data-g="d" data-v="${r}" ${geoBusy ? 'disabled' : ''}><span>${r} km</span>${hasPos ? `<span class="n">${c.d[String(r)] ?? 0}</span>` : ''}</button>`).join('') +
      `<p class="hint">${hasPos ? 'Razdalja od tvoje trenutne lokacije.' : 'Ob izbiri polmera te brskalnik vpraša za dovoljenje za lokacijo (samo v tem zavihku).'}</p>`, 'd')}`;
}
