/** Shape of one public.incidents row as delivered by PostgREST (list columns;
 *  `raw`/`raw_sl` are only present after an on-demand detail fetch or in fixture mode). */
export interface RawIncident {
  id: string;
  source: string;
  region: string | null;
  country: string | null;
  ref: string | null;
  occurred: string | null;
  occurred_time: string | null;
  ts: string | null;
  category: string | null;
  status: string | null;
  title: string | null;
  location: string | null;
  lat: number | null;
  lon: number | null;
  units: string | null;
  crew: number | null;
  vehicles: number | null;
  link: string | null;
  title_sl: string | null;
  first_seen: string | null;
  last_seen: string | null;
  raw?: string | null;
  raw_sl?: string | null;
}

export type Category =
  | 'fire' | 'tech' | 'accident' | 'rescue' | 'ems' | 'weather' | 'exercise' | 'summary' | 'other';
export type Kind = 'dispatch' | 'report' | 'police' | 'weather';
export type Status = 'active' | 'contained' | 'closed' | 'unknown';
export type Country = 'AT' | 'HR';

/** A row enriched once at ingest with everything the renderers need, so no
 *  render ever parses dates or regexes text again. */
export interface Incident extends RawIncident {
  country: Country;
  cat: Category;
  st: Status;
  kind: Kind;
  /** Event time (Europe/Ljubljana wall clock → epoch ms); null when the row has no date. */
  epoch: number | null;
  /** Fallback time for ordering/windows when `epoch` is null. */
  sortMs: number;
  firstSeenMs: number;
  lastSeenMs: number;
  active: boolean;
  /** Text names an air-rescue helicopter (Christophorus, C1x, RK-1/2, Notarzthubschrauber…). */
  heli: boolean;
  /** Text names ground EMS / rescue partners (Notarzt, Rettungsdienst, Rotes Kreuz, hitna pomoć…). */
  ems: boolean;
  /** Folded search haystack (no diacritics, lower-case). */
  hay: string;
  /** True once raw/raw_sl have been fetched (or were present). */
  hasRaw: boolean;
}

export interface ControlRow {
  id: number;
  poll_requested_at: string | null;
  poll_started_at: string | null;
  poll_finished_at: string | null;
  runner_until: string | null;
  note: string | null;
}

export interface SourceStatus {
  source: string;
  ok: boolean | null;
  rows: number | null;
  ms: number | null;
  error: string | null;
  checked_at: string | null;
}
