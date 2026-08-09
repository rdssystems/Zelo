// Service worker do Zellup — instalação do app (PWA), não cache agressivo.
// Regra: nada de dado de negócio (agenda, caixa, comissão) pode ficar velho
// escondido em cache. Só HTML de navegação (network-first, com fallback
// offline) e assets estáticos (/static/, cache-first) passam por aqui —
// fetch HTMX/API é sempre rede pura, sem interceptar.

const CACHE_VERSION = "zellup-v1";
const OFFLINE_URL = "/offline/";
const PRECACHE_URLS = [OFFLINE_URL, "/static/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE_URLS))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_VERSION).map((key) => caches.delete(key)))
      )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Navegação (carregar uma página inteira): sempre tenta rede primeiro —
  // nunca serve HTML velho de agenda/caixa. Só cai pra tela offline se a
  // rede falhar de verdade (sem internet).
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL))
    );
    return;
  }

  // Assets estáticos (CSS/JS/imagens do nosso próprio /static/): cache
  // primeiro, com atualização em segundo plano — é conteúdo versionado por
  // deploy, seguro cachear.
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.open(CACHE_VERSION).then((cache) =>
        cache.match(request).then(
          (cached) =>
            cached ||
            fetch(request).then((response) => {
              cache.put(request, response.clone());
              return response;
            })
        )
      )
    );
  }

  // Qualquer outra coisa (HTMX partial, API, /painel/, /agendar/ etc.) não
  // é interceptada — passa direto pra rede, comportamento padrão do browser.
});
