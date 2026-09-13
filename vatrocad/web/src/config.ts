// Read-only publishable key; RLS permits SELECT on incidents/control and the
// request_poll() RPC only. The service-role key never reaches a browser.
export const SB_URL = 'https://ofzwkanpjhdvlygrhbum.supabase.co';
export const SB_KEY = 'sb_publishable_l3UZaMzvgu_8nknqN-xB7g_VZauptFm';

export const LIST_CAP = 300;            // rows per country on the initial load
export const LOAD_DAYS = 8;             // occurred >= today - 8 d
export const NEW_MINUTES = 30;          // 'NOVO' highlight window (first_seen)
export const HEAD_CHECK_MS = 60_000;    // control-row probe
export const FULL_RELOAD_MS = 600_000;  // full re-fetch (catches deletes)
export const RT_DEBOUNCE_MS = 1_500;    // realtime event coalescing
export const REFRESH_THROTTLE_MS = 60_000;
export const POLL_WAIT_MS = 180_000;    // how long Osveži waits for the poller
export const POLL_STEP_MS = 5_000;
export const TICK_MS = 60_000;          // relative-time refresh

/** Columns fetched for the list. raw/raw_sl are loaded on demand when a row opens. */
export const LIST_COLS = [
  'id', 'source', 'region', 'country', 'ref', 'occurred', 'occurred_time', 'ts',
  'category', 'status', 'title', 'location', 'lat', 'lon', 'units', 'crew', 'vehicles',
  'link', 'title_sl', 'first_seen', 'last_seen',
].join(',');

export const FIXTURE_MODE = typeof location !== 'undefined' && /[?&]fixture=1/.test(location.search);
export const BASE = import.meta.env.BASE_URL || '/';
