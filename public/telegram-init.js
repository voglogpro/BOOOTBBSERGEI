(() => {
  const app = window.Telegram?.WebApp;
  if (!app?.initData) return;

  try {
    app.ready();
    app.expand();
    if (app.isVersionAtLeast?.('6.1')) {
      app.setHeaderColor?.('#21120e');
      app.setBackgroundColor?.('#21120e');
    }
  } catch (error) {
    console.warn('Telegram Mini App initialization failed:', error);
  }
})();
