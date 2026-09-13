import type { Incident, RawIncident } from './types';
import { mapRow } from './classify';

type Listener<T> = (v: T) => void;

export class Emitter<T> {
  private ls = new Set<Listener<T>>();
  on(fn: Listener<T>): () => void { this.ls.add(fn); return () => this.ls.delete(fn); }
  emit(v: T): void { for (const fn of [...this.ls]) { try { fn(v); } catch (e) { console.warn('listener failed', e); } } }
}

export interface ChangeSet { added: string[]; changed: string[]; removed: string[] }

/** Keyed in-memory row set. Emits one ChangeSet per batch, never per row. */
export class Store {
  readonly rows = new Map<string, Incident>();
  readonly changed = new Emitter<ChangeSet>();
  /** Newest last_seen we hold (ISO string, lexically comparable). */
  maxSeen = '';

  get size(): number { return this.rows.size; }

  /** Merge a batch; rows already present keep their fetched raw text. */
  upsert(raws: RawIncident[], opts: { silent?: boolean } = {}): ChangeSet {
    const cs: ChangeSet = { added: [], changed: [], removed: [] };
    for (const r of raws) {
      if (!r || typeof r.id !== 'string') continue;
      const prev = this.rows.get(r.id);
      const next = mapRow(r, prev);
      if (!prev) cs.added.push(r.id);
      else if (sig(prev) !== sig(next)) cs.changed.push(r.id);
      else { // nothing visible changed (e.g. only last_seen moved) – keep object identity stable
        prev.last_seen = next.last_seen; prev.lastSeenMs = next.lastSeenMs;
        this.bumpSeen(r.last_seen);
        continue;
      }
      this.rows.set(r.id, next);
      this.bumpSeen(r.last_seen);
    }
    if (!opts.silent && (cs.added.length || cs.changed.length)) this.changed.emit(cs);
    return cs;
  }

  /** Replace the whole set (full reload): drops ids that are no longer served. */
  replaceAll(raws: RawIncident[]): ChangeSet {
    const keep = new Set(raws.map((r) => r.id));
    const cs = this.upsert(raws, { silent: true });
    for (const id of [...this.rows.keys()]) if (!keep.has(id)) { this.rows.delete(id); cs.removed.push(id); }
    if (cs.added.length || cs.changed.length || cs.removed.length) this.changed.emit(cs);
    return cs;
  }

  remove(ids: string[]): void {
    const removed = ids.filter((id) => this.rows.delete(id));
    if (removed.length) this.changed.emit({ added: [], changed: [], removed });
  }

  /** Attach on-demand raw text to a row (no ChangeSet – the detail view re-renders itself). */
  attachRaw(id: string, raw: string | null, rawSl: string | null): Incident | undefined {
    const i = this.rows.get(id);
    if (!i) return undefined;
    i.raw = raw; i.raw_sl = rawSl; i.hasRaw = true;
    return i;
  }

  private bumpSeen(ls: string | null | undefined): void {
    if (ls && ls > this.maxSeen) this.maxSeen = ls;
  }
}

/** Fields whose change is visible in the list/detail. */
function sig(i: Incident): string {
  return [i.st, i.title, i.title_sl, i.location, i.units, i.epoch, i.lat, i.lon, i.cat, i.link, i.crew, i.vehicles].join('');
}
