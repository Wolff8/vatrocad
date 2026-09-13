import { isPhone } from './dom';

/**
 * A bottom sheet on phones / side drawer on desktop with: history entry (Android
 * back closes it), Escape, body scroll lock, drag-to-close / drag-to-expand on
 * the whole header, focus management and aria-modal semantics.
 */
export class Sheet {
  readonly el: HTMLElement;
  private readonly body: HTMLElement;
  private readonly head: HTMLElement;
  private opener: HTMLElement | null = null;
  private popKey: string;
  private open = false;
  onClose: (() => void) | null = null;

  constructor(el: HTMLElement, private readonly backdrop: HTMLElement) {
    this.el = el;
    this.body = el.querySelector('.sh-body') as HTMLElement;
    this.head = el.querySelector('.sh-head') as HTMLElement;
    this.popKey = `sheet:${el.id}`;
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.hidden = true;
    el.addEventListener('click', (e) => { if ((e.target as HTMLElement).closest('[data-close]')) this.close(); });
    this.bindDrag();
  }

  isOpen(): boolean { return this.open; }

  setContent(html: string): void { this.body.innerHTML = html; }

  show(opener?: HTMLElement | null): void {
    if (opener) this.opener = opener;
    if (this.open) return;
    this.open = true;
    this.el.hidden = false;
    this.backdrop.classList.add('on');
    this.el.classList.remove('expanded');
    requestAnimationFrame(() => this.el.classList.add('open'));
    document.documentElement.classList.add('lock');
    if (isPhone()) this.body.scrollTop = 0;
    try { history.pushState({ [this.popKey]: true }, ''); } catch { /* sandboxed */ }
    const first = this.el.querySelector<HTMLElement>('[data-close]');
    first?.focus({ preventScroll: true });
  }

  /** Close programmatically (no history pop needed – we consume our own state if present). */
  close(fromPop = false): void {
    if (!this.open) return;
    this.open = false;
    this.el.classList.remove('open', 'expanded');
    this.backdrop.classList.remove('on');
    document.documentElement.classList.remove('lock');
    const hide = () => { if (!this.open) this.el.hidden = true; };
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) hide(); else window.setTimeout(hide, 280);
    if (!fromPop) {
      const st = history.state as Record<string, unknown> | null;
      if (st && st[this.popKey]) { this.suppressPop = true; history.back(); }
    }
    this.opener?.focus({ preventScroll: true });
    this.opener = null;
    this.onClose?.();
  }

  private suppressPop = false;
  /** Wire once per app: closes the sheet when the user presses Back / Escape. */
  handlePopstate(): boolean {
    if (this.suppressPop) { this.suppressPop = false; return true; }
    if (this.open) { this.close(true); return true; }
    return false;
  }

  private bindDrag(): void {
    let y0: number | null = null, dy = 0, dragging = false;
    const onStart = (e: PointerEvent) => {
      if (!isPhone() || e.pointerType === 'mouse') return;
      y0 = e.clientY; dy = 0; dragging = true; this.el.style.transition = 'none';
    };
    const onMove = (e: PointerEvent) => {
      if (!dragging || y0 === null) return;
      dy = e.clientY - y0;
      if (dy > 0) this.el.style.transform = `translateY(${dy}px)`;
    };
    const onEnd = () => {
      if (!dragging) return;
      dragging = false; this.el.style.transition = ''; this.el.style.transform = '';
      if (dy > 70) this.close();
      else if (dy < -50) this.el.classList.add('expanded');
      y0 = null;
    };
    this.head.addEventListener('pointerdown', onStart);
    this.head.addEventListener('pointermove', onMove);
    this.head.addEventListener('pointerup', onEnd);
    this.head.addEventListener('pointercancel', onEnd);
    this.head.addEventListener('dblclick', () => this.el.classList.toggle('expanded'));
  }
}
