{% load static %}// HomeOA service worker (PLT-09): offline fallback page only.
// It precaches the public offline page and its stylesheet, and nothing else. Pages and
// data are never written to the cache, so nothing private is kept on the device.
"use strict";
var CACHE = "homeoa-offline-v1";
var OFFLINE_URL = "{% url 'ui:offline' %}";
var PRECACHE = [OFFLINE_URL, "{% static 'homeoa/base.css' %}"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(PRECACHE); })
      .then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; })
        .map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});

self.addEventListener("fetch", function (event) {
  var req = event.request;
  if (req.method !== "GET") return;
  if (req.mode === "navigate") {
    // Network only; on failure show the offline page. The response is never cached.
    event.respondWith(fetch(req).catch(function () { return caches.match(OFFLINE_URL); }));
    return;
  }
  if (PRECACHE.indexOf(new URL(req.url).pathname) !== -1) {
    event.respondWith(fetch(req).catch(function () { return caches.match(req); }));
  }
});
