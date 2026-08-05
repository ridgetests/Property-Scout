// Service worker — network-first so page updates always reach you when online,
// with cached fallback for offline (e.g. on the train). Bump CACHE to force a
// refresh whenever you change the app shell.
const CACHE = "scout-v3";
const SHELL = ["./", "./index.html", "./icon-180.png", "./icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const req = e.request;
  const isImage = req.url.endsWith(".png");
  if (isImage) {
    // icons rarely change — cache-first is fine
    e.respondWith(caches.match(req).then(r => r || fetch(req)));
    return;
  }
  // everything else (HTML, JSON) — network-first, fall back to cache offline
  e.respondWith(
    fetch(req).then(r => {
      const copy = r.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
      return r;
    }).catch(() => caches.match(req))
  );
});
