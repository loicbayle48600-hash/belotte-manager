/* Service worker — mode hors ligne (cache d'abord, mise à jour en arrière-plan) */
const CACHE = 'haccp-v21';
const ASSETS = [
  './',
  './index.html',
  './css/style.css',
  './js/db.js',
  './js/ui.js',
  './js/app.js',
  './js/vendor/xlsx.full.min.js',
  './js/vendor/jspdf.umd.min.js',
  './js/vendor/jspdf.plugin.autotable.min.js',
  './js/vendor/ocr/tesseract.min.js',
  './js/vendor/ocr/worker.min.js',
  './js/vendor/ocr/tesseract-core-simd-lstm.wasm.js',
  './js/vendor/ocr/tesseract-core-lstm.wasm.js',
  './js/vendor/ocr/fra.traineddata.gz',
  './manifest.webmanifest',
  './icons/icon-192.png',
  './icons/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  // Requêtes explicitement « fraîches » (synchro du menu…) : réseau direct,
  // jamais la copie en cache.
  if (e.request.cache === 'no-store') return;

  // Les bibliothèques vendorées (OCR ~14 Mo, xlsx, jspdf) sont immuables :
  // cache seul, sans revalidation réseau en arrière-plan (elles ne changent
  // qu'avec une nouvelle version du cache).
  if (new URL(e.request.url).pathname.includes('/vendor/')) {
    e.respondWith(caches.match(e.request).then(cached => cached || fetch(e.request)));
    return;
  }

  e.respondWith(
    caches.match(e.request).then(cached => {
      const fresh = fetch(e.request).then(resp => {
        if (resp.ok && new URL(e.request.url).origin === location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return resp;
      }).catch(() => cached);
      return cached || fresh;
    })
  );
});
