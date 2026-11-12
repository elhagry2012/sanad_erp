/* سند للأعمال — Service Worker لتطبيق الويب القابل للتثبيت (PWA).
 *
 * التطبيق يعمل مباشرة على الخادم (Streamlit عبر WebSocket) فلا يمكن تشغيله بدون اتصال.
 * لذلك: لا نخزّن صفحات أو بيانات التطبيق إطلاقًا (لا بيانات مالية قديمة من الذاكرة)،
 * ونخزّن فقط الأيقونات وصفحة «لا يوجد اتصال» لتظهر بدل صفحة خطأ المتصفح عند انقطاع الشبكة.
 */
const VERSION = "__VERSION__";
const CACHE = "sanad-shell-" + VERSION;
const OFFLINE = "./sanad-pwa/offline.html";
const SHELL = [OFFLINE, "./sanad-pwa/icon-192.png", "./sanad-pwa/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k.startsWith("sanad-shell-") && k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  // فتح الصفحة فقط: من الشبكة دائمًا، وعند الانقطاع صفحة «لا يوجد اتصال». كل ما عدا ذلك يمر للشبكة دون تدخل.
  if (req.mode === "navigate") {
    event.respondWith(fetch(req).catch(() => caches.match(OFFLINE)));
  }
});
