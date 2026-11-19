// Ferry's service worker does one thing: catch files from the phone's share
// menu, park them, and hand over to the page so you can pick where they go.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "POST" || url.pathname !== "/share-target") return;
  e.respondWith((async () => {
    const form = await e.request.formData();
    const files = form.getAll("files").filter((f) => f && typeof f === "object" && "name" in f);
    await caches.delete("ferry-share");
    const cache = await caches.open("ferry-share");
    let i = 0;
    for (const f of files) {
      await cache.put(`/shared/${i++}`, new Response(f, { headers: {
        "content-type": f.type || "application/octet-stream",
        "x-name": encodeURIComponent(f.name), "x-mtime": String(f.lastModified || 0) } }));
    }
    return Response.redirect(`/?shared=${i}`, 303);
  })());
});
