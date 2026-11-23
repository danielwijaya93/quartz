self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = {
    title: "Quartz",
    body: "You have a new update.",
    tag: "quartz-push",
    url: "/",
    force: false,
  };

  if (event.data) {
    try {
      const parsed = event.data.json();
      if (parsed && typeof parsed === "object") {
        payload = { ...payload, ...parsed };
      }
    } catch {
      const text = event.data.text();
      if (text) payload.body = text;
    }
  }

  event.waitUntil(
    (async () => {
      if (!payload.force) {
        const clients = await self.clients.matchAll({
          type: "window",
          includeUncontrolled: true,
        });
        const anyFocused = clients.some(
          (c) => c.visibilityState === "visible" && c.focused,
        );
        if (anyFocused) return;
      }

      await self.registration.showNotification(payload.title, {
        body: payload.body,
        tag: payload.tag,
        icon: "/icon-192.png",
        badge: "/icon-192.png",
        data: { url: payload.url || "/" },
      });
    })(),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if ("focus" in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(targetUrl);
      }
      return undefined;
    }),
  );
});
