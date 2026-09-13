import { defineConfig, type Plugin } from 'vite';
import { readdirSync, statSync, writeFileSync } from 'node:fs';
import { join, relative } from 'node:path';

const BASE = '/vatrocad/';

/** Writes dist/sw.js after the build with the exact hashed asset list to precache. */
function serviceWorker(): Plugin {
  let outDir = 'dist';
  return {
    name: 'vatrocad-sw',
    apply: 'build',
    configResolved(c) { outDir = c.build.outDir; },
    closeBundle() {
      const files: string[] = [];
      const walk = (dir: string) => {
        for (const f of readdirSync(dir)) {
          const p = join(dir, f);
          if (statSync(p).isDirectory()) { if (f !== 'fixtures') walk(p); continue; }
          const rel = relative(outDir, p).replace(/\\/g, '/');
          if (/\.(js|css|html|svg|png|webmanifest)$/.test(rel) && rel !== 'sw.js') files.push(BASE + rel);
        }
      };
      walk(outDir);
      const version = Date.now().toString(36);
      const sw = `/* VatroCAD service worker – app shell only. Data (/rest/, /realtime/, tiles) is never cached. */
const CACHE = 'vatrocad-shell-${version}';
const SHELL = ${JSON.stringify([BASE, ...files.filter((f) => !f.endsWith('index.html'))])};
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;
  // Never cache API/realtime/tiles/fixtures: network only.
  if (!sameOrigin || /\\/rest\\/|\\/realtime\\/|\\/fixtures\\//.test(url.pathname)) return;
  if (req.mode === 'navigate') {
    // Network-first for the document so deploys show up immediately; shell fallback offline.
    e.respondWith(fetch(req).then((r) => { const c = r.clone(); caches.open(CACHE).then((x) => x.put(${JSON.stringify(BASE)}, c)); return r; })
      .catch(() => caches.match(${JSON.stringify(BASE)})));
    return;
  }
  // Hashed assets: cache-first.
  e.respondWith(caches.match(req).then((hit) => hit || fetch(req).then((r) => {
    if (r.ok && /\\.(js|css|svg|png|webmanifest)$/.test(url.pathname)) { const c = r.clone(); caches.open(CACHE).then((x) => x.put(req, c)); }
    return r;
  })));
});
`;
      writeFileSync(join(outDir, 'sw.js'), sw);
    },
  };
}

export default defineConfig({
  base: BASE,
  plugins: [serviceWorker()],
  build: {
    outDir: 'dist',
    target: 'es2022',
    sourcemap: false,
    cssCodeSplit: false,
    rollupOptions: {
      output: {
        manualChunks: {
          leaflet: ['leaflet', 'leaflet.markercluster'],
        },
      },
    },
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
