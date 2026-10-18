// Telegram's script is loaded only inside the Telegram Mini App and never blocks the site.
// In a regular browser telegram.org may be slow or unreachable, and the page must not wait for it.
(() => {
  const storageKey = '__telegram__initParams';
  const hash = location.hash.replace(/^#/, '');
  let stored = null;
  try { stored = sessionStorage.getItem(storageKey); } catch { /* Storage is off. */ }
  const insideTelegram = hash.includes('tgWebApp') || Boolean(stored) || Boolean(window.TelegramWebviewProxy);
  if (!insideTelegram) return;

  // The site rewrites the hash while switching screens. Save Telegram's launch data now,
  // so the script still finds it even if it arrives after the first screen change.
  if (hash.includes('tgWebApp')) {
    try {
      const query = hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : hash;
      const params = Object.fromEntries(new URLSearchParams(query));
      const previous = stored ? JSON.parse(stored) : {};
      sessionStorage.setItem(storageKey, JSON.stringify({ ...previous, ...params }));
    } catch { /* Telegram will read the hash itself. */ }
  }

  function init() {
    const app = window.Telegram?.WebApp;
    if (!app?.initData) return;
    try {
      app.ready();
      app.expand();
      if (app.isVersionAtLeast?.('6.1')) {
        app.setHeaderColor?.('#1a0f0b');
        app.setBackgroundColor?.('#1a0f0b');
      }
      document.dispatchEvent(new CustomEvent('telegram-ready'));
    } catch (error) {
      console.warn('Telegram Mini App initialization failed:', error);
    }
  }

  const script = document.createElement('script');
  script.src = 'https://telegram.org/js/telegram-web-app.js';
  script.async = true;
  script.onload = init;
  document.head.appendChild(script);
})();
