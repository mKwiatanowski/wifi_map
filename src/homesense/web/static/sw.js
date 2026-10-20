// HomeSense service worker (served at /sw.js with Service-Worker-Allowed: /).
//
// Privacy: only static assets (/static/*) are cached. Pages are network-only with an offline
// notice as fallback, so personal data is never stored in the cache or shown offline to someone
// else. /api/*, the SSE stream and /grafana/* are not intercepted at all.
const CACHE = "homesense-static-v3";
const PRECACHE = [
  "/static/css/app.css",
  "/static/js/app.js",
  "/static/js/map.js",
  "/static/vendor/htmx.min.js",
  "/static/vendor/alpine.min.js",
  "/static/vendor/chart.umd.min.js",
  "/static/icons/favicon.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/manifest.webmanifest",
];
const BYPASS = ["/api/", "/grafana/", "/login", "/logout", "/setup"];

const OFFLINE_HTML = `<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brak połączenia – HomeSense</title><link rel="stylesheet" href="/static/css/app.css"></head>
<body class="bare"><main class="auth"><section class="card auth-card"><h1>Brak połączenia</h1>
<p>Nie można połączyć się z serwerem HomeSense. Sprawdź połączenie z siecią domową i spróbuj ponownie.</p>
</section></main></body></html>`;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// App code and styles change with each deployment: network-first, so a new app.js/app.css is used
// on the first load after an update (the cache only serves offline).
const NETWORK_FIRST = ["/static/js/", "/static/css/"];

async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (response.ok) cache.put(request, response.clone());
    return response;
  } catch (error) {
    const cached = await cache.match(request);
    if (cached) return cached;
    throw error;
  }
}

async function staticAsset(request) {
  // Stale-while-revalidate: answer from the cache, refresh it in the background.
  const cache = await caches.open(CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => cached);
  return cached || network;
}

async function page(request) {
  try {
    return await fetch(request);
  } catch {
    return new Response(OFFLINE_HTML, { status: 503, headers: { "Content-Type": "text/html; charset=utf-8" } });
  }
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (BYPASS.some((prefix) => url.pathname.startsWith(prefix))) return;
  if (NETWORK_FIRST.some((prefix) => url.pathname.startsWith(prefix))) {
    event.respondWith(networkFirst(request));
  } else if (url.pathname.startsWith("/static/")) {
    event.respondWith(staticAsset(request));
  } else if (request.mode === "navigate") {
    event.respondWith(page(request));
  }
});
