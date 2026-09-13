/**
 * All wall-clock handling goes through Intl with the Europe/Ljubljana zone.
 * No fixed offsets anywhere: DST (last Sunday of March / October) is derived
 * from the IANA data the browser ships.
 */
export const TZ = 'Europe/Ljubljana';

interface Parts { y: number; m: number; d: number; h: number; mi: number; s: number }

const partsFmt = new Intl.DateTimeFormat('en-US', {
  timeZone: TZ, hourCycle: 'h23',
  year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit',
});

export function zonedParts(epoch: number): Parts {
  const out: Partial<Parts> = {};
  for (const p of partsFmt.formatToParts(new Date(epoch))) {
    const v = Number(p.value);
    switch (p.type) {
      case 'year': out.y = v; break;
      case 'month': out.m = v; break;
      case 'day': out.d = v; break;
      case 'hour': out.h = v === 24 ? 0 : v; break;
      case 'minute': out.mi = v; break;
      case 'second': out.s = v; break;
    }
  }
  return { y: out.y ?? 1970, m: out.m ?? 1, d: out.d ?? 1, h: out.h ?? 0, mi: out.mi ?? 0, s: out.s ?? 0 };
}

/** Offset of Europe/Ljubljana from UTC at `epoch`, in minutes (60 in winter, 120 in summer). */
export function tzOffsetMin(epoch: number): number {
  const p = zonedParts(epoch);
  const asUtc = Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi, p.s);
  return Math.round((asUtc - Math.floor(epoch / 1000) * 1000) / 60_000);
}

export function tzAbbrev(epoch: number): string {
  return tzOffsetMin(epoch) === 120 ? 'CEST' : 'CET';
}

const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})/;
const TIME_RE = /^(\d{1,2}):(\d{2})/;

/**
 * Wall-clock `YYYY-MM-DD` + `HH:MM` in Europe/Ljubljana → epoch ms.
 * Missing/invalid time counts as 00:00 (ages out sooner, never fakes freshness).
 * Returns null for a missing or malformed date.
 */
export function localToEpoch(date: string | null | undefined, time?: string | null): number | null {
  if (!date) return null;
  const dm = DATE_RE.exec(date);
  if (!dm) return null;
  const y = Number(dm[1]), m = Number(dm[2]), d = Number(dm[3]);
  if (m < 1 || m > 12 || d < 1 || d > 31) return null;
  let h = 0, mi = 0;
  const tm = time ? TIME_RE.exec(time) : null;
  if (tm) { h = Number(tm[1]); mi = Number(tm[2]); }
  if (h > 23 || mi > 59) { h = 0; mi = 0; }
  const guess = Date.UTC(y, m - 1, d, h, mi);
  const off1 = tzOffsetMin(guess);
  let epoch = guess - off1 * 60_000;
  const off2 = tzOffsetMin(epoch);
  if (off2 !== off1) epoch = guess - off2 * 60_000;
  return epoch;
}

/** ISO-8601 (with or without offset) → epoch ms, or null. */
export function parseIso(s: string | null | undefined): number | null {
  if (!s) return null;
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}

const p2 = (n: number) => String(n).padStart(2, '0');

export function fmtTime(epoch: number): string {
  const p = zonedParts(epoch);
  return `${p2(p.h)}:${p2(p.mi)}`;
}
export function fmtClock(epoch: number): string {
  const p = zonedParts(epoch);
  return `${p2(p.h)}:${p2(p.mi)}:${p2(p.s)}`;
}
/** Slovenian short date: `10. 9.` */
export function fmtDate(epoch: number): string {
  const p = zonedParts(epoch);
  return `${p.d}. ${p.m}.`;
}
/** `10. 9. 2026, 14:05` */
export function fmtDateTime(epoch: number): string {
  const p = zonedParts(epoch);
  return `${p.d}. ${p.m}. ${p.y}, ${p2(p.h)}:${p2(p.mi)}`;
}
/** `YYYY-MM-DD` of the local (Europe/Ljubljana) calendar day. */
export function dayKey(epoch: number): string {
  const p = zonedParts(epoch);
  return `${p.y}-${p2(p.m)}-${p2(p.d)}`;
}

/** Relative time in Slovenian: 'pravkar', 'pred 12 min', 'pred 3 h', 'pred 2 d', 'čez 40 min'. */
export function relTime(epoch: number, now: number): string {
  const diff = now - epoch;
  const ahead = diff < 0;
  const m = Math.round(Math.abs(diff) / 60_000);
  let s: string;
  if (m < 1) return 'pravkar';
  if (m < 60) s = `${m} min`;
  else if (m < 24 * 60) s = `${Math.round(m / 60)} h`;
  else s = `${Math.round(m / (24 * 60))} d`;
  return ahead ? `čez ${s}` : `pred ${s}`;
}

/** Minutes since `epoch`, rounded, never negative. */
export function minutesSince(epoch: number, now: number): number {
  return Math.max(0, Math.round((now - epoch) / 60_000));
}

export type Band = 'future' | 'hour' | 'today' | 'yesterday' | 'older' | 'weather';
export const BAND_LABEL: Record<Band, string> = {
  future: 'Prihajajoče',
  hour: 'Zadnja ura',
  today: 'Danes',
  yesterday: 'Včeraj',
  older: 'Starejše',
  weather: 'Vremenska opozorila',
};

/** Which list group a time falls into, relative to `now`, on the local calendar. */
export function timeBand(epoch: number, now: number): Band {
  if (epoch > now + 5 * 60_000) return 'future';
  if (now - epoch <= 60 * 60_000) return 'hour';
  const today = dayKey(now);
  const k = dayKey(epoch);
  if (k === today) return 'today';
  if (isYesterday(k, today)) return 'yesterday';
  return 'older';
}

function isYesterday(k: string, today: string): boolean {
  // Compare calendar days as UTC dates so a DST-shortened day still counts as one day.
  const a = Date.UTC(+k.slice(0, 4), +k.slice(5, 7) - 1, +k.slice(8, 10));
  const b = Date.UTC(+today.slice(0, 4), +today.slice(5, 7) - 1, +today.slice(8, 10));
  return b - a === 86_400_000;
}

/** Cutoff calendar date string (`YYYY-MM-DD`, local) `days` days before `now`. */
export function cutoffDate(now: number, days: number): string {
  return dayKey(now - days * 86_400_000);
}
