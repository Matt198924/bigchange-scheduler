const CACHE='van-parts-hub-v1';
self.addEventListener('install',e=>{self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil((async()=>{const keys=await caches.keys(); await Promise.all(keys.map(k=>caches.delete(k))); await self.clients.claim();})());});
self.addEventListener('fetch',e=>{if(e.request.method!=='GET'||e.request.url.includes('/api/')) return; e.respondWith(fetch(e.request).then(r=>{const copy=r.clone(); caches.open(CACHE).then(c=>c.put(e.request,copy)); return r;}).catch(()=>caches.match(e.request)));});
