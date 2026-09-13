import { describe, expect, it } from 'vitest';
import { cutoffDate, dayKey, fmtDate, fmtDateTime, fmtTime, localToEpoch, relTime, timeBand, tzAbbrev, tzOffsetMin } from './time';

describe('Europe/Ljubljana wall clock → epoch', () => {
  it('summer (CEST, +02:00)', () => {
    expect(localToEpoch('2026-07-15', '12:00')).toBe(Date.UTC(2026, 6, 15, 10, 0));
  });
  it('winter (CET, +01:00) – the bug the fixed +02:00 offset had', () => {
    expect(localToEpoch('2026-01-15', '12:00')).toBe(Date.UTC(2026, 0, 15, 11, 0));
  });
  it('day after DST ends (25 Oct 2026 → 26 Oct is CET)', () => {
    expect(localToEpoch('2026-10-26', '08:30')).toBe(Date.UTC(2026, 9, 26, 7, 30));
    expect(tzAbbrev(localToEpoch('2026-10-26', '08:30')!)).toBe('CET');
  });
  it('DST end day: 02:30 is ambiguous – resolves to a valid instant on that day', () => {
    const t = localToEpoch('2026-10-25', '02:30')!;
    expect(dayKey(t)).toBe('2026-10-25');
    expect(fmtTime(t)).toBe('02:30');
  });
  it('DST start day: 02:30 does not exist – lands on the same local day', () => {
    const t = localToEpoch('2026-03-29', '02:30')!;
    expect(dayKey(t)).toBe('2026-03-29');
  });
  it('before/after DST start (29 Mar 2026 02:00 → 03:00)', () => {
    expect(localToEpoch('2026-03-29', '01:30')).toBe(Date.UTC(2026, 2, 29, 0, 30));
    expect(localToEpoch('2026-03-29', '04:00')).toBe(Date.UTC(2026, 2, 29, 2, 0));
  });
  it('missing / malformed time → midnight local; missing date → null', () => {
    expect(localToEpoch('2026-09-10', null)).toBe(Date.UTC(2026, 8, 9, 22, 0));
    expect(localToEpoch('2026-09-10', '24:00')).toBe(Date.UTC(2026, 8, 9, 22, 0));
    expect(localToEpoch('2026-09-10', '9:05')).toBe(Date.UTC(2026, 8, 10, 7, 5));
    expect(localToEpoch(null, '12:00')).toBeNull();
    expect(localToEpoch('garbage', '12:00')).toBeNull();
    expect(localToEpoch('2026-13-40', '12:00')).toBeNull();
  });
  it('offset helper', () => {
    expect(tzOffsetMin(Date.UTC(2026, 6, 1))).toBe(120);
    expect(tzOffsetMin(Date.UTC(2026, 0, 1))).toBe(60);
  });
});

describe('formatting', () => {
  const t = Date.UTC(2026, 8, 10, 12, 5); // 14:05 CEST
  it('time/date/datetime in Slovenian style', () => {
    expect(fmtTime(t)).toBe('14:05');
    expect(fmtDate(t)).toBe('10. 9.');
    expect(fmtDateTime(t)).toBe('10. 9. 2026, 14:05');
    expect(tzAbbrev(t)).toBe('CEST');
  });
  it('local day boundaries follow the zone, not UTC', () => {
    expect(dayKey(Date.UTC(2026, 8, 10, 22, 30))).toBe('2026-09-11');
    expect(dayKey(Date.UTC(2026, 0, 10, 23, 30))).toBe('2026-01-11');
  });
  it('relative time', () => {
    const now = t;
    expect(relTime(now - 20_000, now)).toBe('pravkar');
    expect(relTime(now - 12 * 60_000, now)).toBe('pred 12 min');
    expect(relTime(now - 3 * 3_600_000, now)).toBe('pred 3 h');
    expect(relTime(now - 2 * 86_400_000, now)).toBe('pred 2 d');
    expect(relTime(now + 40 * 60_000, now)).toBe('čez 40 min');
  });
});

describe('time bands', () => {
  const now = Date.UTC(2026, 8, 10, 12, 0); // Thu 14:00 CEST
  it('hour / today / yesterday / older / future', () => {
    expect(timeBand(now - 30 * 60_000, now)).toBe('hour');
    expect(timeBand(now - 5 * 3_600_000, now)).toBe('today');
    expect(timeBand(localToEpoch('2026-09-10', '00:10')!, now)).toBe('today');
    expect(timeBand(localToEpoch('2026-09-09', '23:50')!, now)).toBe('yesterday');
    expect(timeBand(localToEpoch('2026-09-08', '23:50')!, now)).toBe('older');
    expect(timeBand(now + 3_600_000, now)).toBe('future');
  });
  it('yesterday across the DST change (26 Oct 2026 looks back at 25 Oct)', () => {
    const n = localToEpoch('2026-10-26', '10:00')!;
    expect(timeBand(localToEpoch('2026-10-25', '01:00')!, n)).toBe('yesterday');
    expect(timeBand(localToEpoch('2026-10-24', '23:00')!, n)).toBe('older');
  });
  it('cutoff date is on the local calendar', () => {
    expect(cutoffDate(Date.UTC(2026, 8, 10, 23, 0), 8)).toBe('2026-09-03');
  });
});
