// DemoCut service worker (C10). Only here so phones offer "Install" / "Add to Home Screen".
// It caches nothing: runs, events and videos always come live from the API.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
