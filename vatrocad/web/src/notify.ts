import type { Incident } from './types';
import { dispatchTitle } from './classify';
import { tryStorage } from './ui/dom';

const KEY = 'vatrocad.notify';
const ALERT_CATS = new Set(['fire', 'accident', 'rescue']);

/** Opt-in in-tab notifications for new ACTIVE fire / accident / rescue rows. */
export class Notifier {
  enabled = false;

  constructor() {
    this.enabled = tryStorage(() => localStorage.getItem(KEY) === '1', false) && this.permitted();
  }

  supported(): boolean { return typeof Notification !== 'undefined'; }
  permitted(): boolean { return this.supported() && Notification.permission === 'granted'; }

  async toggle(): Promise<'on' | 'off' | 'denied' | 'unsupported'> {
    if (!this.supported()) return 'unsupported';
    if (this.enabled) { this.enabled = false; tryStorage(() => localStorage.setItem(KEY, '0'), undefined); return 'off'; }
    let perm: NotificationPermission = Notification.permission;
    if (perm === 'default') { try { perm = await Notification.requestPermission(); } catch { perm = 'denied'; } }
    if (perm !== 'granted') return 'denied';
    this.enabled = true; tryStorage(() => localStorage.setItem(KEY, '1'), undefined);
    return 'on';
  }

  /** Fire at most one notification per batch; ignore rows first seen long ago (e.g. initial load). */
  notifyNew(rows: Incident[], now: number): void {
    if (!this.enabled || !this.permitted()) return;
    const hits = rows.filter((i) => i.active && ALERT_CATS.has(i.cat) && now - i.firstSeenMs < 30 * 60_000);
    if (!hits.length) return;
    const first = hits[0]!;
    const t = dispatchTitle(first);
    const title = hits.length === 1 ? `${t.head} · ${t.subject}` : `${hits.length} novih aktivnih dogodkov`;
    const body = hits.length === 1 ? (first.location || first.source) : hits.map((h) => dispatchTitle(h).subject).slice(0, 3).join(' · ');
    try {
      const n = new Notification(title, { body, tag: 'vatrocad-new', icon: `${import.meta.env.BASE_URL}icon-192.png` });
      n.onclick = () => { window.focus(); location.hash = `#id=${encodeURIComponent(first.id)}`; n.close(); };
    } catch { /* some browsers only allow SW notifications */ }
  }
}
