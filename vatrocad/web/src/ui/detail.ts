import type { Incident } from '../types';
import { AT_STATE, CAT, HR_REG, KIND, T } from '../labels';
import { atExplain, colorOf, dispatchTitle, iconOf, isNew, isTranslated, narrativeOf, safeLink, statusClass, statusLabel, unitChips } from '../classify';
import { fmtDateTime, relTime, tzAbbrev } from '../time';
import { NEW_MINUTES } from '../config';
import { esc } from './dom';
import { ic } from './sprite';

const kv = (pairs: (readonly [string, string] | null)[]): string =>
  `<dl class="kv">${pairs.filter((p): p is readonly [string, string] => !!p).map(([l, v]) => `<div><dt>${esc(l)}</dt><dd>${v}</dd></div>`).join('')}</dl>`;

export function detailHTML(i: Incident, now: number, opts: { loadingRaw: boolean }): string {
  const t = dispatchTitle(i);
  const sl = statusLabel(i);
  const when = i.epoch !== null ? `${fmtDateTime(i.epoch)} ${tzAbbrev(i.epoch)} · ${relTime(i.epoch, now)}` : '—';
  const region = i.country === 'AT' ? AT_STATE[i.region || ''] : HR_REG[i.region || ''];
  const { chips } = i.cat === 'weather' ? { chips: [] } : unitChips(i.units, 40);
  const nar = narrativeOf(i);
  const link = safeLink(i.link);
  const expl = atExplain(i);
  const crew = [i.crew != null ? `${i.crew} gasilcev` : null, i.vehicles != null ? `${i.vehicles} vozil` : null].filter(Boolean).join(' · ');
  return `
  <header class="dh">
    <span class="bdg" style="--c:${colorOf(i)}">${ic(iconOf(i))}${esc(CAT[i.cat].label)}</span>
    ${sl ? `<span class="pill ${statusClass(i)}">${esc(sl)}</span>` : ''}
    ${isNew(i, now, NEW_MINUTES) ? `<span class="pill new">${T.novo}</span>` : ''}
    <span class="tag">${ic(KIND[i.kind].icon)}${esc(KIND[i.kind].label)}</span>
    <h2 class="dt"><span class="hd">${esc(t.head)}</span> · ${esc(t.subject)}${t.place ? ` · <span class="pl">${esc(t.place)}</span>` : ''}</h2>
  </header>
  ${kv([
    ['Kraj', esc(i.location || '—')],
    [i.country === 'AT' ? 'Zvezna dežela' : 'Regija', esc(region || i.region || '—')],
    ['Čas dogodka', esc(when)],
    ['Stanje', esc(sl || '—')],
    crew ? ['Posadka / vozila', esc(crew)] : null,
    i.cat === 'weather' && i.units ? ['Resnost', esc(i.units)] : null,
    ['Referenca', esc(i.ref || '—')],
    ['Vir', esc(i.source)],
    ['Država', i.country === 'AT' ? 'Avstrija' : 'Hrvaška'],
  ])}
  ${expl ? `<div class="exp"><b>${i.country === 'AT' ? 'Koda in stopnja' : 'Opomba'}:</b> ${esc(expl)}${i.heli ? ' · v besedilu je omenjen helikopter / zdravnik NMP' : ''}</div>` : ''}
  ${chips.length ? `<div class="exp"><b>Enote:</b><div class="units">${chips.map((u) => `<span class="uc">${esc(u)}</span>`).join('')}</div></div>` : ''}
  <section class="nar" aria-label="Opis">
    ${!i.hasRaw && opts.loadingRaw ? `<div class="mtnote" role="status">${T.loadingRaw}</div>` : ''}
    ${nar.sl ? `<div class="raw"><b>Opis:</b> ${esc(nar.sl)}</div>` : nar.orig ? `<div class="raw">${esc(nar.orig)}</div>` : (i.hasRaw ? `<div class="mtnote">${T.rawUnavailable}</div>` : '')}
    ${nar.sl && nar.orig ? `<details class="orig"><summary>${T.original(T.langName(i.country))}</summary><div>${esc(nar.orig)}</div></details>` : ''}
    ${isTranslated(i) ? `<div class="mtnote">${T.mtNote(T.langOf(i.country))}</div>` : ''}
  </section>
  <div class="acts">
    ${i.lat !== null ? `<button type="button" class="btn pri" data-map="${esc(i.id)}">${ic('map')} ${T.showOnMap}</button>` : ''}
    <button type="button" class="btn" data-share="${esc(i.id)}">${ic('share')} ${T.share}</button>
    ${link ? `<a class="btn" href="${esc(link)}" target="_blank" rel="noopener noreferrer">${ic('ext')} ${T.openSource}</a>` : ''}
  </div>`;
}

/** Plain-text summary for Web Share / clipboard. */
export function shareText(i: Incident, now: number): string {
  const t = dispatchTitle(i);
  const parts = [`${t.head} · ${t.subject}${t.place ? ' · ' + t.place : ''}`];
  if (i.location) parts.push(i.location);
  if (i.epoch !== null) parts.push(`${fmtDateTime(i.epoch)} (${relTime(i.epoch, now)})`);
  const sl = statusLabel(i); if (sl) parts.push(sl);
  parts.push(`Vir: ${i.source}`);
  return parts.join('\n');
}
