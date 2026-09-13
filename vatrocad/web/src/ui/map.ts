import L from 'leaflet';
import 'leaflet.markercluster';
import 'leaflet/dist/leaflet.css';
import 'leaflet.markercluster/dist/MarkerCluster.css';
import type { Incident } from '../types';
import { colorOf, dispatchTitle, iconOf } from '../classify';
import { CAT } from '../labels';
import { fmtTime, relTime } from '../time';
import { esc, isPhone } from './dom';
import { ic } from './sprite';

const ESRI_BASE = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}';
const ESRI_REF = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}';

interface Entry { marker: L.Marker; sig: string }

/**
 * Leaflet map with clustered, category-coloured markers keyed by id. Nothing is
 * rendered while the container is hidden; `sync()` is cheap when nothing changed.
 */
export class IncidentMap {
  private map: L.Map | null = null;
  private cluster: L.MarkerClusterGroup | null = null;
  private entries = new Map<string, Entry>();
  private dirty: Incident[] | null = null;
  private fitted = false;
  private wantFit = false;
  private selected: string | null = null;
  private userDot: L.CircleMarker | null = null;
  private now = Date.now();

  /** `onSelect(id, explicit)`: explicit=false when a popup merely opened, true for the popup's button. */
  constructor(private readonly el: HTMLElement, private readonly onSelect: (id: string, explicit: boolean) => void) {}

  private visible(): boolean { return this.el.clientWidth > 0 && this.el.clientHeight > 0; }

  private init(): void {
    if (this.map) return;
    const map = L.map(this.el, { zoomControl: true, attributionControl: true, preferCanvas: true })
      .setView([46.2, 15.6], 7);
    L.tileLayer(ESRI_BASE, { maxZoom: 16, attribution: 'Tiles &copy; Esri', updateWhenIdle: true, crossOrigin: true }).addTo(map);
    L.tileLayer(ESRI_REF, { maxZoom: 16, updateWhenIdle: true, crossOrigin: true }).addTo(map);
    this.cluster = L.markerClusterGroup({
      chunkedLoading: true, maxClusterRadius: 44, disableClusteringAtZoom: 12, spiderfyOnMaxZoom: true,
      showCoverageOnHover: false, removeOutsideVisibleBounds: true,
      iconCreateFunction: (c) => {
        const n = c.getChildCount();
        const kids = c.getAllChildMarkers() as (L.Marker & { _vc?: { act: boolean; col: string } })[];
        let act = 0; const cols = new Map<string, number>();
        for (const k of kids) { if (k._vc?.act) act++; if (k._vc) cols.set(k._vc.col, (cols.get(k._vc.col) || 0) + 1); }
        const top = [...cols.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || '#9aa8b6';
        const size = n < 10 ? 34 : n < 100 ? 40 : 46;
        return L.divIcon({
          html: `<div class="clu ${act ? 'act' : ''}" style="--c:${top}"><span>${n}</span></div>`,
          className: 'cluw', iconSize: [size, size],
        });
      },
    });
    map.addLayer(this.cluster);
    map.on('popupopen', (e) => {
      const id = (e.popup as L.Popup & { _vcId?: string })._vcId;
      if (id) this.onSelect(id, false);
    });
    // 'Podrobnosti' button inside a popup: explicit request to open the detail view.
    this.el.addEventListener('click', (e) => {
      const b = (e.target as HTMLElement).closest<HTMLElement>('[data-detail]');
      if (b) { e.preventDefault(); this.onSelect(b.dataset.detail!, true); }
    });
    this.map = map;
  }

  /** Called when the container becomes visible (tab switch, desktop layout). */
  reveal(): void {
    if (!this.visible()) return;
    this.init();
    this.map!.invalidateSize();
    if (this.dirty) { const d = this.dirty; this.dirty = null; this.sync(d, this.now, true); }
    else if (this.wantFit) this.fit();
  }

  /** Reconcile markers with `rows`; no wipe/rebuild. */
  sync(rows: Incident[], now: number, fit = false): void {
    this.now = now;
    if (!this.visible()) { this.dirty = rows; if (fit) this.wantFit = true; return; }
    this.init();
    const cluster = this.cluster!;
    const wanted = new Set<string>();
    const add: L.Marker[] = [];
    for (const i of rows) {
      if (i.lat === null || i.lon === null) continue;
      wanted.add(i.id);
      const sig = markerSig(i, this.selected === i.id);
      const ex = this.entries.get(i.id);
      if (ex) {
        if (ex.sig !== sig) { ex.marker.setIcon(pinIcon(i, this.selected === i.id)); ex.sig = sig; (ex.marker as MarkerX)._vc = { act: i.active, col: colorOf(i) }; }
        const ll = ex.marker.getLatLng();
        if (Math.abs(ll.lat - i.lat) > 1e-6 || Math.abs(ll.lng - i.lon) > 1e-6) ex.marker.setLatLng([i.lat, i.lon]);
        continue;
      }
      const m = L.marker([i.lat, i.lon], { icon: pinIcon(i, this.selected === i.id), keyboard: false, title: dispatchTitle(i).subject }) as MarkerX;
      m._vc = { act: i.active, col: colorOf(i) };
      m._vcId = i.id;
      const popup = L.popup({ closeButton: true, autoPanPaddingBottomRight: [10, isPhone() ? 320 : 10], maxWidth: 260 }) as L.Popup & { _vcId?: string };
      popup._vcId = i.id;
      popup.setContent(() => popupHTML(i, this.now));
      m.bindPopup(popup);
      this.entries.set(i.id, { marker: m, sig });
      add.push(m);
    }
    const del: L.Marker[] = [];
    for (const [id, e] of this.entries) if (!wanted.has(id)) { del.push(e.marker); this.entries.delete(id); }
    if (del.length) cluster.removeLayers(del);
    if (add.length) cluster.addLayers(add);
    if (fit || !this.fitted) this.fit();
  }

  fit(): void {
    if (!this.map || !this.visible()) { this.wantFit = true; return; }
    this.wantFit = false;
    const pts: L.LatLngExpression[] = [];
    for (const e of this.entries.values()) pts.push(e.marker.getLatLng());
    if (!pts.length) return;
    this.map.fitBounds(L.latLngBounds(pts), { padding: [28, 28], maxZoom: 10 });
    this.fitted = true;
  }

  setSelected(id: string | null, rows: Map<string, Incident>): void {
    const prev = this.selected;
    this.selected = id;
    for (const pid of [prev, id]) {
      if (!pid) continue;
      const e = this.entries.get(pid); const i = rows.get(pid);
      if (e && i) { e.marker.setIcon(pinIcon(i, pid === id)); e.sig = markerSig(i, pid === id); }
    }
  }

  /** Fly to a row and open its popup (spiderfies the cluster if needed). */
  focus(i: Incident): void {
    if (!this.map || i.lat === null || i.lon === null) return;
    const e = this.entries.get(i.id);
    const target = L.latLng(i.lat, i.lon);
    const zoom = Math.max(this.map.getZoom(), 12);
    this.map.flyTo(target, zoom, { duration: matchMedia('(prefers-reduced-motion: reduce)').matches ? 0 : 0.5 });
    if (e && this.cluster) {
      window.setTimeout(() => {
        this.cluster!.zoomToShowLayer(e.marker, () => { e.marker.openPopup(); });
      }, 600);
    }
  }

  setUserLocation(pos: { lat: number; lon: number } | null): void {
    if (!this.map) { if (pos) this.pendingPos = pos; return; }
    if (this.userDot) { this.userDot.remove(); this.userDot = null; }
    if (pos) {
      this.userDot = L.circleMarker([pos.lat, pos.lon], { radius: 7, color: '#fff', weight: 2, fillColor: '#3ddc7e', fillOpacity: .95 })
        .bindTooltip('Moja lokacija').addTo(this.map);
    }
  }
  private pendingPos: { lat: number; lon: number } | null = null;
  flushPending(): void { if (this.pendingPos && this.map) { this.setUserLocation(this.pendingPos); this.pendingPos = null; } }

  invalidate(): void { this.map?.invalidateSize(); }
}

type MarkerX = L.Marker & { _vc?: { act: boolean; col: string }; _vcId?: string };

function markerSig(i: Incident, sel: boolean): string {
  return `${i.cat}|${i.st}|${i.active ? 1 : 0}|${sel ? 1 : 0}|${iconOf(i)}`;
}

function pinIcon(i: Incident, sel: boolean): L.DivIcon {
  const cls = ['mk', sel ? 'sel' : '', i.active ? 'act' : '', i.cat === 'exercise' ? 'ex' : ''].join(' ');
  const s = sel ? 38 : 30;
  return L.divIcon({
    className: 'mkw', iconSize: [s, s], iconAnchor: [s / 2, s], popupAnchor: [0, -s + 4],
    html: `<div class="${cls}" style="--c:${colorOf(i)}">${ic(iconOf(i))}</div>`,
  });
}

function popupHTML(i: Incident, now: number): string {
  const t = dispatchTitle(i);
  return `<div class="mpop"><span class="bdg" style="--c:${colorOf(i)}">${ic(iconOf(i))}${esc(CAT[i.cat].label)}</span>
    <div class="pt">${esc(t.head)} · ${esc(t.subject)}</div>
    <div class="pm">${esc(i.location || '')}${i.epoch !== null ? ` · ${fmtTime(i.epoch)} · ${relTime(i.epoch, now)}` : ''}</div>
    <button type="button" class="btn pbtn" data-detail="${esc(i.id)}">Podrobnosti</button></div>`;
}
