(() => {
  const assets = [
    { path: 'assets/scene-01-locked-loop-web.mp4', kind: 'video', size: 6314401 },
    { path: 'assets/scene-02-original-forward-loop-web.mp4', kind: 'video', size: 3926004 },
    { path: 'assets/scene-03-loop-web.mp4', kind: 'video', size: 1356294 },
    { path: 'assets/transition-01-02-full-4s-web.mp4', kind: 'video', size: 2665735 },
    { path: 'assets/transition-02-03-full-4s-web.mp4', kind: 'video', size: 2264628 },
    { path: 'storyboard/01-real-fountain-v2.webp', kind: 'image', size: 234810 },
    { path: 'storyboard/02-hands-strawberry-approved.webp', kind: 'image', size: 194134 },
    { path: 'storyboard/03-fountain-left-approved.webp', kind: 'image', size: 239106 },
    { path: 'assets/fountain-poster-v2.webp', kind: 'image', size: 189846 },
    { path: 'assets/real-event-fountain.jpg', kind: 'image', size: 187622 }
  ];
  const loader = document.getElementById('site-loader');
  const status = document.getElementById('loader-status');
  const percent = document.getElementById('loader-percent');
  const bar = document.querySelector('.loader-progress');
  const fill = document.getElementById('loader-progress-fill');
  const skip = document.getElementById('loader-skip');
  const retry = document.getElementById('loader-retry');
  const controller = new AbortController();
  const sizes = new Map(assets.map(asset => [asset.path, asset.size]));
  const loaded = new Map(assets.map(asset => [asset.path, 0]));
  const objectUrls = [];
  let state = 'loading';
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  const main = document.querySelector('main');
  main?.setAttribute('aria-busy', 'true');

  function updateProgress(complete = false) {
    const total = [...sizes.values()].reduce((sum, size) => sum + size, 0);
    const done = [...loaded.values()].reduce((sum, size) => sum + size, 0);
    const value = complete ? 100 : Math.min(99, Math.floor(done / total * 100));
    fill.style.width = `${value}%`;
    percent.textContent = `${value}%`;
    bar.setAttribute('aria-valuenow', String(value));
  }

  function setLoaded(path, bytes) {
    loaded.set(path, Math.min(bytes, sizes.get(path)));
    updateProgress();
  }

  function attachVideo(path, source) {
    const video = [...document.querySelectorAll('video[data-video]')].find(node => node.dataset.video === path);
    if (!video) throw new Error(`Video element missing: ${path}`);
    if (video.classList.contains('scene-video')) {
      video.addEventListener('loadeddata', () => video.classList.add('is-ready'), { once: true });
    }
    return new Promise((resolve, reject) => {
      let finished = false;
      const done = error => {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        video.removeEventListener('loadeddata', onReady);
        video.removeEventListener('error', onError);
        if (error) reject(error);
        else resolve();
      };
      const onReady = () => done();
      const onError = () => done(new Error(`Video cannot be decoded: ${path}`));
      const timer = setTimeout(() => done(), 6000);
      video.addEventListener('loadeddata', onReady, { once: true });
      video.addEventListener('error', onError, { once: true });
      video.preload = 'auto';
      video.src = source;
      video.dataset.loaded = 'true';
      video.load();
    });
  }

  async function downloadVideo(asset) {
    const response = await fetch(asset.path, { signal: controller.signal });
    if (!response.ok) throw new Error(`Video download failed: ${asset.path}`);
    const responseSize = Number(response.headers.get('Content-Length'));
    if (responseSize > 0) sizes.set(asset.path, responseSize);
    let blob;
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      const chunks = [];
      let received = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        received += value.byteLength;
        setLoaded(asset.path, received);
      }
      blob = new Blob(chunks, { type: 'video/mp4' });
    } else {
      blob = await response.blob();
    }
    if (!blob.size) throw new Error(`Empty video: ${asset.path}`);
    const objectUrl = URL.createObjectURL(blob);
    objectUrls.push(objectUrl);
    await attachVideo(asset.path, objectUrl);
    setLoaded(asset.path, sizes.get(asset.path));
  }

  function loadImage(asset) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = async () => {
        try { await image.decode?.(); } catch { /* The loaded image is still usable. */ }
        setLoaded(asset.path, sizes.get(asset.path));
        resolve();
      };
      image.onerror = () => reject(new Error(`Image download failed: ${asset.path}`));
      image.src = asset.path;
    });
  }

  function loadWithoutWaiting() {
    if (state !== 'loading' && state !== 'error') return;
    state = 'skipped';
    controller.abort();
    for (const asset of assets.filter(item => item.kind === 'video')) {
      const video = [...document.querySelectorAll('video[data-video]')].find(node => node.dataset.video === asset.path);
      if (video?.dataset.loaded) continue;
      video.addEventListener('loadeddata', () => video.classList.add('is-ready'), { once: true });
      video.preload = 'auto';
      video.src = asset.path;
      video.dataset.loaded = 'true';
      video.load();
    }
    status.textContent = 'Открываем сайт';
    resolveReady({ complete: false });
  }

  skip.addEventListener('click', loadWithoutWaiting);
  retry.addEventListener('click', () => location.reload());
  const slowTimer = setTimeout(() => { if (state === 'loading') skip.hidden = false; }, 12000);

  window.sitePreloader = {
    ready,
    get state() { return state; },
    get progress() { return Number(bar.getAttribute('aria-valuenow')); },
    reveal() {
      clearTimeout(slowTimer);
      main?.setAttribute('aria-busy', 'false');
      document.body.classList.remove('is-loading');
      loader.classList.add('is-complete');
      setTimeout(() => loader.remove(), 700);
    }
  };

  async function start() {
    await Promise.all(assets.map(asset => asset.kind === 'video' ? downloadVideo(asset) : loadImage(asset)));
    status.textContent = 'Готовим первый кадр';
    if (document.fonts?.ready) {
      await Promise.race([document.fonts.ready, new Promise(resolve => setTimeout(resolve, 6000))]);
    }
    if (state !== 'loading') return;
    state = 'complete';
    status.textContent = 'Всё готово';
    updateProgress(true);
    clearTimeout(slowTimer);
    resolveReady({ complete: true });
  }

  start().catch(error => {
    if (state !== 'loading') return;
    state = 'error';
    controller.abort();
    clearTimeout(slowTimer);
    status.textContent = 'Не удалось загрузить все материалы';
    skip.hidden = false;
    retry.hidden = false;
    console.error('Site preload failed:', error);
  });

  addEventListener('pagehide', () => {
    controller.abort();
    objectUrls.forEach(url => URL.revokeObjectURL(url));
  }, { once: true });
})();
