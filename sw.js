/* Ilmaradar service worker.
 *
 * Strategy:
 *   - App shell (HTML, manifest, icons): stale-while-revalidate.
 *     User sees the last cached version instantly, even offline; an
 *     updated copy is fetched in the background and used on next load.
 *   - API calls (open-meteo, NOAA): network-only. We let the page's
 *     localStorage layer handle offline data, since the SW can't parse
 *     the responses meaningfully.
 *
 * Bump CACHE_VERSION whenever the shell changes so old caches are purged.
 */

const CACHE_VERSION = 'v1';
const CACHE_NAME = 'ilmaradar-' + CACHE_VERSION;

const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon-180.png',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-512-maskable.png',
  './icons/apple-touch-icon.png',
  './icons/favicon-32.png',
  './icons/favicon-16.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isApiRequest(url) {
  return (
    url.hostname.endsWith('open-meteo.com') ||
    url.hostname.endsWith('swpc.noaa.gov') ||
    url.hostname.endsWith('fonts.googleapis.com') ||
    url.hostname.endsWith('fonts.gstatic.com')
  );
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);

  // Same-origin assets: stale-while-revalidate
  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.open(CACHE_NAME).then(async (cache) => {
        const cached = await cache.match(req, { ignoreSearch: false });
        const networkFetch = fetch(req)
          .then((resp) => {
            if (resp && resp.status === 200 && resp.type === 'basic') {
              cache.put(req, resp.clone());
            }
            return resp;
          })
          .catch(() => null);

        // Serve cached if we have it, otherwise wait for network.
        return cached || (await networkFetch) || new Response('Offline', {
          status: 503,
          headers: { 'Content-Type': 'text/plain' },
        });
      })
    );
    return;
  }

  // External fonts: cache opportunistically so the app shell still renders offline
  if (url.hostname.endsWith('fonts.googleapis.com') ||
      url.hostname.endsWith('fonts.gstatic.com')) {
    event.respondWith(
      caches.open(CACHE_NAME + '-fonts').then(async (cache) => {
        const cached = await cache.match(req);
        if (cached) return cached;
        try {
          const resp = await fetch(req);
          if (resp && resp.status === 200) cache.put(req, resp.clone());
          return resp;
        } catch (e) {
          return cached || Response.error();
        }
      })
    );
    return;
  }

  // Weather APIs: pass through to the network. The page caches parsed
  // data in localStorage, which is the right layer for "show last reading
  // when offline".
});
