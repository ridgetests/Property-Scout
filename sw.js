// Minimal service worker: app shell cached for instant/offline open,
// properties.json fetched network-first so data stays fresh but still
// works with no signal (e.g. on the tube).
const SHELL = "scout-shell-v1";
const SHELL_FILES = ["./", "./index.html", "./icon-180.png", "./icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(SHELL_FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== SHELL).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", e => {
  const url = e.request.url;
  if (url.endsWith("properties.json")) {
    // network-first: fresh data when online, cached copy when not
    e.respondWith(
      fetch(e.request).then(r => {
        const copy = r.clone();
        caches.open(SHELL).then(c => c.put(e.request, copy));
        return r;
      }).catch(() => caches.match(e.request))
    );
  } else {
    // cache-first for the shell
    e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
  }
});
