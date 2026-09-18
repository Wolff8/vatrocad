import type { Incident } from '../types';
import type { Group } from '../filters';
import { CAT, KIND, T, regionShort } from '../labels';
import { colorOf, dispatchTitle, iconOf, isNew, statusClass, statusLabel, unitChips } from '../classify';
import { fmtDate, fmtTime, relTime } from '../time';
import { NEW_MINUTES } from '../config';
import { esc } from './dom';
import { ic } from './sprite';

export interface ListCallbacks {
  onSelect: (id: string, el: HTMLElement) => void;
  onShowMap: (id: string) => void;
  onMore: () => void;
}

/**
 * Keyed list renderer: every row element is kept in a Map by id and reused;
 * only rows whose signature changed are re-rendered, others are moved in place.
 * Relative times are patched by `tick()` without touching the DOM structure.
 */
export class IncidentList {
  private rows = new Map<string, HTMLElement>();
  private heads = new Map<string, HTMLElement>();
  private more: HTMLButtonElement | null = null;
  private empty: HTMLElement | null = null;
  private firstPaint = true;
  private selected: string | null = null;

  constructor(private readonly root: HTMLElement, private readonly cb: ListCallbacks) {
    root.addEventListener('click', (e) => {
      const t = e.target as HTMLElement;
      const mapBtn = t.closest<HTMLElement>('[data-map]');
      if (mapBtn) { e.stopPropagation(); cb.onShowMap(mapBtn.dataset.map!); return; }
      if (t.closest('[data-more]')) { cb.onMore(); return; }
      const row = t.closest<HTMLElement>('.rw');
      if (row) cb.onSelect(row.dataset.id!, row);
    });
    root.addEventListener('keydown', (e) => {
      const row = (e.target as HTMLElement).closest<HTMLElement>('.rw');
      if (!row) return;
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); cb.onSelect(row.dataset.id!, row); }
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp' || e.key === 'Home' || e.key === 'End') {
        e.preventDefault();
        const all = [...root.querySelectorAll<HTMLElement>('.rw')];
        const idx = all.indexOf(row);
        const next = e.key === 'ArrowDown' ? all[idx + 1] : e.key === 'ArrowUp' ? all[idx - 1] : e.key === 'Home' ? all[0] : all[all.length - 1];
        if (next) { this.roving(next); next.focus(); }
      }
    });
  }

  private roving(el: HTMLElement): void {
    for (const r of this.rows.values()) r.tabIndex = -1;
    el.tabIndex = 0;
  }

  setSelected(id: string | null): void {
    if (this.selected && this.selected !== id) this.rows.get(this.selected)?.setAttribute('aria-selected', 'false');
    this.selected = id;
    if (id) this.rows.get(id)?.setAttribute('aria-selected', 'true');
  }

  scrollTo(id: string): void {
    const el = this.rows.get(id);
    el?.scrollIntoView({ block: 'center', behavior: 'smooth' });
  }

  rowEl(id: string): HTMLElement | undefined { return this.rows.get(id); }

  /** Reconcile the DOM with `groups` (already sorted/limited). */
  render(groups: Group[], now: number, remaining: number, emptyText: string | null): void {
    const root = this.root;
    const wanted = new Set<string>();
    let cursor: ChildNode | null = root.firstChild;
    // Walk the desired order; `cursor` is the next not-yet-placed existing child.
    const place = (node: HTMLElement) => {
      if (node === cursor) cursor = node.nextSibling;
      else root.insertBefore(node, cursor);
    };
    // Empty state / headers / rows in order
    if (this.empty) { this.empty.remove(); this.empty = null; }
    if (this.more) { this.more.remove(); this.more = null; }
    let first = true;
    for (const g of groups) {
      let h = this.heads.get(g.key);
      if (!h) {
        h = document.createElement('div');
        h.className = 'gh'; h.setAttribute('role', 'presentation');
        this.heads.set(g.key, h);
      }
      h.innerHTML = `<span>${esc(g.label)}</span><b>${g.items.length}</b>`;
      place(h);
      for (const i of g.items) {
        wanted.add(i.id);
        let el = this.rows.get(i.id);
        const sig = rowSig(i, now);
        if (!el) {
          el = document.createElement('article');
          el.className = 'rw';
          el.setAttribute('role', 'option');
          el.dataset.id = i.id;
          el.tabIndex = first ? 0 : -1;
          el.setAttribute('aria-selected', String(this.selected === i.id));
          el.dataset.sig = sig;
          el.innerHTML = rowHTML(i, now);
          if (!this.firstPaint) el.classList.add('enter');
          this.rows.set(i.id, el);
        } else if (el.dataset.sig !== sig) {
          el.dataset.sig = sig;
          el.innerHTML = rowHTML(i, now);
        }
        applyState(el, i, now);
        place(el);
        first = false;
      }
    }
    for (const [k, h] of this.heads) if (!groups.some((g) => g.key === k)) { h.remove(); this.heads.delete(k); }
    for (const [id, el] of this.rows) if (!wanted.has(id)) { el.remove(); this.rows.delete(id); }
    // Trailing nodes: more button / empty
    if (remaining > 0) {
      this.more = document.createElement('button');
      this.more.className = 'more'; this.more.type = 'button'; this.more.dataset.more = '1';
      this.more.textContent = T.more(remaining);
      root.appendChild(this.more);
    }
    if (!wanted.size && emptyText) {
      this.empty = document.createElement('div');
      this.empty.className = 'empty'; this.empty.setAttribute('role', 'status');
      this.empty.innerHTML = emptyText;
      root.appendChild(this.empty);
    }
    // Ensure exactly one row is tabbable
    if (wanted.size && ![...this.rows.values()].some((r) => r.tabIndex === 0)) {
      const f = root.querySelector<HTMLElement>('.rw'); if (f) f.tabIndex = 0;
    }
    this.firstPaint = false;
  }

  /** Cheap periodic patch: relative times and the NOVO / fresh state. */
  tick(byId: (id: string) => Incident | undefined, now: number): void {
    for (const [id, el] of this.rows) {
      const i = byId(id); if (!i) continue;
      const rel = el.querySelector<HTMLElement>('[data-rel]');
      if (rel && i.epoch !== null) { const s = relTime(i.epoch, now); if (rel.textContent !== s) rel.textContent = s; }
      applyState(el, i, now);
    }
  }
}

function rowSig(i: Incident, now: number): string {
  return `${i.st}|${i.title_sl ?? ''}|${i.title ?? ''}|${i.location ?? ''}|${i.units ?? ''}|${i.epoch}|${i.cat}|${i.kind}|${i.heli ? 1 : 0}${i.ems ? 1 : 0}|${isNew(i, now, NEW_MINUTES) ? 1 : 0}`;
}

function applyState(el: HTMLElement, i: Incident, now: number): void {
  el.classList.toggle('act', i.active);
  el.classList.toggle('fire-live', i.active && i.cat === 'fire' && i.st === 'active');
  el.classList.toggle('ex', i.cat === 'exercise');
  el.classList.toggle('rep', i.kind === 'report' || i.kind === 'police');
  const nw = isNew(i, now, NEW_MINUTES);
  el.classList.toggle('new', nw);
  const badge = el.querySelector<HTMLElement>('.novo');
  if (badge) badge.hidden = !nw;
}

export function rowHTML(i: Incident, now: number): string {
  const t = dispatchTitle(i);
  const sl = statusLabel(i);
  const { chips, more } = i.cat === 'weather' ? { chips: [], more: 0 } : unitChips(i.units, 4);
  const reg = regionShort(i.country, i.region);
  const time = i.epoch !== null ? fmtTime(i.epoch) : '—';
  const date = i.epoch !== null ? fmtDate(i.epoch) : '';
  const rel = i.epoch !== null ? relTime(i.epoch, now) : 'čas neznan';
  return `
    <div class="bar" style="--c:${colorOf(i)}"></div>
    <div class="ico" style="--c:${colorOf(i)}" title="${esc(CAT[i.cat].label)}">${ic(iconOf(i))}</div>
    <div class="bd">
      <div class="tt"><span class="hd">${esc(t.head)}</span><span class="sep">·</span><span class="sj">${esc(t.subject)}</span>${t.place ? `<span class="sep">·</span><span class="pl">${esc(t.place)}</span>` : ''}</div>
      <div class="meta">
        ${sl ? `<span class="pill ${statusClass(i)}">${esc(sl)}</span>` : ''}
        <span class="novo pill new" ${isNew(i, now, NEW_MINUTES) ? '' : 'hidden'}>${T.novo}</span>
        <span class="lo">${ic('pin')}<span>${esc(i.location || '—')}</span></span>
        ${reg ? `<span class="tag">${esc(reg)}</span>` : ''}
        ${i.heli ? `<span class="tag heli" title="V besedilu je omenjen reševalni helikopter">${ic('heli')}helikopter</span>` : ''}
        ${i.ems && !i.heli ? `<span class="tag ems" title="V besedilu so omenjeni reševalci / NMP">${ic('ems')}reševalci</span>` : ''}
        <span class="tag src" title="${esc(KIND[i.kind].hint)}">${ic(KIND[i.kind].icon)}${esc(i.source)}</span>
      </div>
      ${chips.length ? `<div class="units">${chips.map((u) => `<span class="uc">${esc(u)}</span>`).join('')}${more ? `<span class="uc more">+${more}</span>` : ''}</div>` : ''}
    </div>
    <div class="tm">
      <b>${esc(time)}</b><span class="dt">${esc(date)}</span><span class="rel" data-rel>${esc(rel)}</span>
      ${i.lat !== null ? `<button type="button" class="mapb" data-map="${esc(i.id)}" aria-label="${T.showOnMap}" title="${T.showOnMap}">${ic('map')}</button>` : ''}
    </div>`;
}
