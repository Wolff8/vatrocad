import type { Category, Kind, Status } from './types';

/** Category → colour token, icon id, labels. */
export const CAT: Record<Category, { color: string; icon: string; label: string; short: string; head: string }> = {
  fire:     { color: '#ff7a45', icon: 'fire',     label: 'Požar',                     short: 'Požar',     head: 'POŽAR' },
  tech:     { color: '#5aa9ec', icon: 'tech',     label: 'Tehnična intervencija',     short: 'Tehnično',  head: 'TEHNIČNA' },
  accident: { color: '#ffd84d', icon: 'accident', label: 'Prometna nesreča',          short: 'Nesreča',   head: 'NESREČA' },
  rescue:   { color: '#3ad3c4', icon: 'rescue',   label: 'Reševanje oseb / živali',   short: 'Reševanje', head: 'REŠEVANJE' },
  ems:      { color: '#c48cf5', icon: 'ems',      label: 'Nujna medicinska pomoč',    short: 'NMP',       head: 'NMP' },
  weather:  { color: '#ffc233', icon: 'weather',  label: 'Vremensko opozorilo',       short: 'Vreme',     head: 'VREME' },
  exercise: { color: '#8fa0b0', icon: 'exercise', label: 'Vaja',                      short: 'Vaja',      head: 'VAJA' },
  summary:  { color: '#a3b1bf', icon: 'report',   label: 'Povzetek / pregled',        short: 'Pregled',   head: 'PREGLED' },
  other:    { color: '#9aa8b6', icon: 'other',    label: 'Drugo',                     short: 'Drugo',     head: 'DRUGO' },
};
export const CATEGORIES: Category[] = ['fire', 'accident', 'tech', 'rescue', 'ems', 'weather', 'exercise', 'summary', 'other'];

export const STATUS: Record<Status, string> = {
  active: 'AKTIVNO', contained: 'LOKALIZIRANO', closed: 'ZAKLJUČENO', unknown: '',
};
export const STATUS_LABEL_REPORT = 'POROČILO';

export const KIND: Record<Kind, { label: string; icon: string; hint: string }> = {
  dispatch: { label: 'Dispečer', icon: 'radio',   hint: 'dispečerski dnevnik / dnevnik enote' },
  report:   { label: 'Poročila', icon: 'report',  hint: 'poročilo enote ali novinarska kronika' },
  police:   { label: 'Policija', icon: 'police',  hint: 'uradno policijsko sporočilo' },
  weather:  { label: 'Vreme',    icon: 'weather', hint: 'Meteoalarm opozorilo' },
};
export const KINDS: Kind[] = ['dispatch', 'report', 'police', 'weather'];

export const HR_REG: Record<string, string> = {
  sib: 'Šibenik-Knin', med: 'Medžimurje', zag: 'Zagreb', kaz: 'Karlovac', sdz: 'Split-Dalmacija',
  smz: 'Sisak', zdz: 'Zadar', kkz: 'Koprivnica', vaz: 'Varaždin', psz: 'Požega', bbz: 'Bjelovar',
  kzz: 'Krapina', vpz: 'Virovitica', obz: 'Osijek-Baranja', bpz: 'Slavonski Brod', isz: 'Istra',
  pgz: 'Reka', lsz: 'Lika-Senj', dnz: 'Dubrovnik', vsz: 'Vukovar', nat: 'nacionalno',
};
export const AT_STATE: Record<string, string> = {
  ooe: 'Zgornja Avstrija', noe: 'Spodnja Avstrija', stmk: 'Štajerska', ktn: 'Koroška',
};
export const AT_SHORT: Record<string, string> = { ooe: 'OÖ', noe: 'NÖ', stmk: 'Štajerska', ktn: 'Koroška' };
export const AT_BORDER = new Set(['stmk', 'ktn']);

export const COUNTRY_LABEL: Record<string, string> = { AT: 'Avstrija', HR: 'Hrvaška', all: 'Vse' };

export function regionLabel(country: string, region: string | null): string {
  if (!region) return '';
  return (country === 'AT' ? AT_STATE[region] : HR_REG[region]) ?? region;
}
export function regionShort(country: string, region: string | null): string {
  if (!region) return '';
  return (country === 'AT' ? AT_SHORT[region] : HR_REG[region]) ?? region;
}

export const AT_KIND: Record<string, string> = {
  B: 'B = Brand — požar',
  T: 'T = Technischer Einsatz — tehnična intervencija',
  S: 'S = Schadstoff — nevarne snovi / iztekanje',
  SOF: 'SOF = Sonderfall — druge naloge (podpora, izvidovanje, pomoč drugi službi)',
  U: 'U = Übung — vaja, ni resnična intervencija',
};
export const AT_LEVEL: Record<string, string> = {
  '0': 'stopnja 0 — brez nevarnosti, rutinska naloga',
  '1': 'stopnja 1 — manjši obseg, ena enota',
  '2': 'stopnja 2 — srednji obseg, več enot, ogrožene osebe',
  '3': 'stopnja 3 — večji obseg, enote iz sosednjih krajev',
  '4': 'stopnja 4 — velik dogodek, številne enote',
};

export const WINDOWS: { h: number; label: string }[] = [
  { h: 1, label: '1 h' }, { h: 6, label: '6 h' }, { h: 12, label: '12 h' },
  { h: 24, label: '24 h' }, { h: 72, label: '3 d' }, { h: 168, label: '7 d' },
];
export const RADII = [25, 50, 100];

/** UI strings kept in one place (Slovenian). */
export const T = {
  title: 'VatroCAD · dispečerska konzola',
  refresh: 'Osveži',
  refreshing: 'Osvežujem…',
  fetching: 'Pobiram s virov…',
  fresh: 'Sveže s virov · ni novega',
  pollerIdle: 'Pobiralnik trenutno ne teče – podatki iz baze',
  pollerTimeout: 'Pobiralnik se ne odziva · prikazani so zadnji shranjeni podatki',
  sbDown: 'Supabase ne odgovarja',
  throttled: 'Počakaj minuto pred naslednjo osvežitvijo',
  live: 'v živo', polling: 'osveževanje', offline: 'ni povezave', connecting: 'povezujem…',
  pollerRunning: 'pobiralnik teče', pollerStopped: 'pobiralnik miruje',
  lastPoll: (min: number) => (min < 1 ? 'pobrano pravkar' : `pobrano pred ${min} min`),
  noPoll: 'brez podatka o pobiranju',
  empty: 'Ni intervencij za izbrane filtre.',
  emptyHint: (label: string) => `V zadnjih ${label} ni bilo nič — razširi časovno okno ali počisti filtre.`,
  more: (n: number) => `Prikaži več (${n})`,
  novo: 'NOVO',
  showOnMap: 'Pokaži na zemljevidu',
  share: 'Deli',
  copied: 'Povezava kopirana',
  openSource: 'Odpri vir',
  original: (lang: string) => `Izvirnik (${lang})`,
  mtNote: (lang: string) => `samodejni prevod iz ${lang}`,
  loadingRaw: 'Nalagam besedilo…',
  rawUnavailable: 'Besedilo ni na voljo.',
  nearby: 'V bližini',
  geoDenied: 'Lokacija ni dovoljena',
  notifyOn: 'Obvestila vklopljena',
  notifyOff: 'Obvestila izklopljena',
  notifyDenied: 'Brskalnik obvestil ne dovoli',
  filters: 'Filtri',
  clearFilters: 'Počisti filtre',
  sources: 'Viri',
  list: 'Seznam', map: 'Zemljevid',
  langOf: (c: string) => (c === 'AT' ? 'nemščine' : 'hrvaščine'),
  langName: (c: string) => (c === 'AT' ? 'nemško' : 'hrvaško'),
} as const;
