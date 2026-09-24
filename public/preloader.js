(() => {
  // The first screen opens as soon as its poster, fonts and hero video are ready
  // (or after a short cap). Everything else downloads in the background, in the
  // order a visitor needs it, and plays from memory once it arrives.
  const heroPoster = 'storyboard/01-real-fountain-v2.webp';
  const heroVideo = 'assets/scene-01-locked-loop-web.mp4';
  const backgroundVideos = [
    'assets/transition-01-02-full-4s-web.mp4',
    'assets/scene-02-original-forward-loop-web.mp4',
    'assets/transition-02-03-full-4s-web.mp4',
    'assets/scene-03-loop-web.mp4'
  ];
  const backgroundImages = [
    'storyboard/02-hands-strawberry-approved.webp',
    'storyboard/03-fountain-left-approved.webp',
    'assets/fountain-poster-v2.webp',
    'assets/real-event-fountain.jpg'
  ];
  const minimumShow = 650;
  const heroVideoWait = 2600;
  const hardCap = 4200;

  const loader = document.getElementById('site-loader');
  const bar = document.querySelector('.loader-progress');
  const fill = document.getElementById('loader-progress-fill');
  const main = document.querySelector('main');
  const controller = new AbortController();
  const objectUrls = [];
  const readyVideos = new Set();
  const startedAt = performance.now();
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const saveData = Boolean(navigator.connection?.saveData);
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });
  let steps = 0;
  let progressTimer;
  main?.setAttribute('aria-busy', 'true');

  const videoFor = path => [...document.querySelectorAll('video[data-video]')].find(node => node.dataset.video === path);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

  function setProgress(value) {
    const rounded = Math.round(Math.min(100, value));
    if (fill) fill.style.width = `${rounded}%`;
    bar?.setAttribute('aria-valuenow', String(rounded));
  }
  function stepDone() {
    steps += 1;
  }
  function tickProgress() {
    const elapsed = performance.now() - startedAt;
    setProgress(Math.max(steps / 3 * 100, Math.min(90, elapsed / heroVideoWait * 90)));
  }

  function markReady(video, path) {
    readyVideos.add(path);
    video.classList.add('is-ready');
    video.dataset.ready = 'true';
  }

  function loadImage(path) {
    return new Promise(resolve => {
      const image = new Image();
      image.onload = async () => {
        try { await image.decode?.(); } catch { /* A loaded image is still usable. */ }
        resolve(true);
      };
      image.onerror = () => resolve(false);
      image.src = path;
    });
  }

  // The hero streams straight from the server so it can start before it has fully arrived.
  function startHeroVideo() {
    const video = videoFor(heroVideo);
    if (!video || reduceMotion) return Promise.resolve(false);
    return new Promise(resolve => {
      const done = ok => {
        video.removeEventListener('canplay', onReady);
        video.removeEventListener('error', onError);
        resolve(ok);
      };
      const onReady = () => { markReady(video, heroVideo); done(true); };
      const onError = () => done(false);
      video.addEventListener('canplay', onReady, { once: true });
      video.addEventListener('error', onError, { once: true });
      video.preload = 'auto';
      video.src = heroVideo;
      video.dataset.loaded = 'true';
      video.load();
    });
  }

  async function downloadVideo(path) {
    const video = videoFor(path);
    // The visitor already reached this scene and it is streaming; keep that stream.
    if (!video || video.dataset.loaded) return;
    const response = await fetch(path, { signal: controller.signal });
    if (!response.ok) throw new Error(`Video download failed: ${path}`);
    const blob = await response.blob();
    if (!blob.size) throw new Error(`Empty video: ${path}`);
    if (video.dataset.loaded) return;
    const objectUrl = URL.createObjectURL(blob);
    objectUrls.push(objectUrl);
    await new Promise(resolve => {
      const timer = setTimeout(resolve, 6000);
      video.addEventListener('loadeddata', () => {
        clearTimeout(timer);
        markReady(video, path);
        resolve();
      }, { once: true });
      video.addEventListener('error', () => { clearTimeout(timer); resolve(); }, { once: true });
      video.preload = 'auto';
      video.src = objectUrl;
      video.dataset.loaded = 'true';
      video.load();
    });
  }

  // Let the hero finish buffering before other clips compete for the connection.
  function heroSettled() {
    const video = videoFor(heroVideo);
    if (!video || video.readyState >= 4 || video.error) return Promise.resolve();
    return Promise.race([
      new Promise(resolve => video.addEventListener('canplaythrough', resolve, { once: true })),
      wait(5000)
    ]);
  }

  async function loadBackground() {
    backgroundImages.forEach(loadImage);
    if (reduceMotion || saveData) return;
    await heroSettled();
    for (const path of backgroundVideos) {
      try {
        await downloadVideo(path);
      } catch (error) {
        if (error.name === 'AbortError') return;
        console.warn(error.message);
      }
    }
  }

  window.sitePreloader = {
    ready,
    isVideoReady: path => readyVideos.has(path),
    reveal() {
      clearInterval(progressTimer);
      setProgress(100);
      main?.setAttribute('aria-busy', 'false');
      document.body.classList.remove('is-loading');
      loader?.classList.add('is-complete');
      setTimeout(() => loader?.remove(), 700);
      loadBackground();
    }
  };

  async function start() {
    progressTimer = setInterval(tickProgress, 80);
    const fontsReady = document.fonts?.ready
      ? Promise.race([document.fonts.ready, wait(2500)])
      : Promise.resolve();
    const critical = Promise.all([
      loadImage(heroPoster).then(stepDone),
      fontsReady.then(stepDone),
      Promise.race([startHeroVideo(), wait(heroVideoWait)]).then(stepDone)
    ]);
    await Promise.race([critical, wait(hardCap)]);
    const shown = performance.now() - startedAt;
    if (shown < minimumShow) await wait(minimumShow - shown);
    resolveReady();
  }

  start();

  addEventListener('pagehide', () => {
    controller.abort();
    objectUrls.forEach(url => URL.revokeObjectURL(url));
  }, { once: true });
})();
