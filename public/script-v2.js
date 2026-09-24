(() => {
  const config = window.SITE_CONFIG || {};
  const scenes = [...document.querySelectorAll('.scene')];
  const dots = [...document.querySelectorAll('.scene-dots button')];
  const navLinks = [...document.querySelectorAll('[data-go]')];
  const currentLabel = document.getElementById('current-index');
  const railProgress = document.getElementById('rail-progress');
  const dialog = document.getElementById('contact-dialog');
  const offerDialog = document.getElementById('offer-dialog');
  const form = document.getElementById('lead-form');
  const status = document.getElementById('form-status');
  const submitButton = document.getElementById('submit-button');
  const journeyVideos = {
    1: document.getElementById('journey-video'),
    2: document.getElementById('journey-video-23')
  };
  const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
  let active = 0;
  let traveling = false;
  let journeyActive = false;
  let primedSceneVideo = null;
  let journeyOutgoingVideo = null;
  let lockedUntil = 0;
  let touchStart = null;
  let wheelAmount = 0;
  let wheelTimer;

  function setText(selector, value) {
    const node = document.querySelector(selector);
    if (node && value) node.textContent = value;
  }

  setText('[data-brand]', config.brand);
  setText('[data-price]', config.price);
  setText('[data-region]', config.region);
  document.title = (config.brand || 'Шоколадный фонтан') + ' — праздник со вкусом';
  document.getElementById('year').textContent = new Date().getFullYear();
  if (config.phone) document.querySelectorAll('[data-phone]').forEach(link => { link.href = 'tel:' + String(config.phone).replace(/[^\d+]/g, ''); });
  const modalOpen = () => dialog.open || offerDialog.open;

  function prepareVideo(scene) {
    scene.querySelectorAll('video[data-video]').forEach(video => {
      if (video.dataset.loaded) return;
      video.dataset.loaded = 'true';
      video.preload = 'auto';
      video.src = video.dataset.video;
      video.addEventListener('loadeddata', () => {
        video.classList.add('is-ready');
        playCurrent();
      }, { once: true });
      video.addEventListener('error', () => video.classList.remove('is-ready'));
      video.load();
    });
  }

  function playCurrent() {
    scenes.forEach((scene, index) => {
      const video = scene.querySelector('video');
      if (!video) return;
      const shouldPlay = (journeyActive && (video === primedSceneVideo || video === journeyOutgoingVideo)) ||
        (!journeyActive && (index === active || (traveling && scene.classList.contains('is-simple-entering'))));
      if (shouldPlay && !modalOpen() && !reduceMotion.matches && video.dataset.loaded && !video.error) video.play().catch(() => {});
      else video.pause();
    });
  }

  function commitScene(next) {
    scenes[active].classList.remove('is-active');
    scenes[active].setAttribute('aria-hidden', 'true');
    active = next;
    scenes[active].classList.add('is-active');
    scenes[active].removeAttribute('aria-hidden');
    currentLabel.textContent = String(active + 1).padStart(2, '0');
    railProgress.style.height = ((active + 1) / scenes.length * 100) + '%';
    dots.forEach((dot, index) => {
      dot.classList.toggle('is-current', index === active);
      dot.setAttribute('aria-current', index === active ? 'step' : 'false');
    });
    navLinks.forEach(link => link.classList.toggle('is-current', Number(link.dataset.go) === active));
    history.replaceState(null, '', '#' + scenes[active].id);
    prepareVideo(scenes[active]);
    playCurrent();
  }

  function simpleTo(next) {
    const leaving = scenes[active];
    const entering = scenes[next];
    traveling = true;
    prepareVideo(entering);
    leaving.classList.add('is-simple-exiting');
    entering.classList.add('is-simple-entering');
    playCurrent();
    setTimeout(() => {
      commitScene(next);
      leaving.classList.remove('is-simple-exiting');
      entering.classList.remove('is-simple-entering');
      traveling = false;
      lockedUntil = Date.now() + 260;
    }, 620);
  }

  function journeyTo(next) {
    const journeyVideo = journeyVideos[next];
    if (!journeyVideo) { simpleTo(next); return; }
    const leaving = scenes[active];
    const entering = scenes[next];
    const sceneVideo = entering.querySelector('video');
    traveling = true;
    journeyActive = true;
    primedSceneVideo = null;
    journeyOutgoingVideo = leaving.querySelector('video');
    prepareVideo(entering);
    entering.classList.add('is-video-entering');
    playCurrent();

    let finished = false;
    let startTimer;
    let endTimer;
    let copyTimer;
    let journeyFrameRequest;
    let journeyAnimationFrame;
    let outgoingPauseTimer;

    // The second journey crosses a wide hall. A portrait crop must travel
    // from the strawberry side to the fountain entering from the left.
    function updateJourneyCrop() {
      if (next !== 2) return;
      if (!matchMedia('(max-width:700px)').matches) {
        journeyVideo.style.objectPosition = '';
        return;
      }
      const duration = Number.isFinite(journeyVideo.duration) ? journeyVideo.duration : 4;
      const progress = Math.max(0, Math.min(1, journeyVideo.currentTime / duration));
      const eased = progress * progress * (3 - 2 * progress);
      journeyVideo.style.objectPosition = `${(48 - 32 * eased).toFixed(2)}% center`;
    }

    // Seek while the travel footage still covers the next scene. Seeking at the
    // handoff used to show a poster or an undecoded first frame for ~1 second.
    let entryCued = false;
    function cueScene() {
      if (finished || entryCued || !sceneVideo || sceneVideo.readyState < 1) return;
      entryCued = true;
      if (sceneVideo.currentTime !== 0) sceneVideo.currentTime = 0;
    }
    if (sceneVideo?.readyState >= 1) cueScene();
    else sceneVideo?.addEventListener('loadedmetadata', cueScene, { once: true });

    function revealScene() {
      if (!finished && journeyActive) entering.classList.add('is-video-settling');
    }
    function primeScene() {
      if (finished || primedSceneVideo === sceneVideo || !sceneVideo) return;
      primedSceneVideo = sceneVideo;
      sceneVideo.addEventListener('playing', revealScene, { once: true });
      playCurrent();
      if (!sceneVideo.paused && sceneVideo.readyState >= 2) revealScene();
    }
    function onJourneyProgress() {
      updateJourneyCrop();
      const remaining = journeyVideo.duration - journeyVideo.currentTime;
      if (Number.isFinite(remaining) && remaining <= 0.28) primeScene();
    }
    function onJourneyFrame() {
      if (finished) return;
      onJourneyProgress();
      journeyFrameRequest = journeyVideo.requestVideoFrameCallback(onJourneyFrame);
    }
    function onJourneyAnimationFrame() {
      if (finished) return;
      onJourneyProgress();
      journeyAnimationFrame = requestAnimationFrame(onJourneyAnimationFrame);
    }
    function stopJourneyFrameWatch() {
      if (journeyFrameRequest != null) journeyVideo.cancelVideoFrameCallback(journeyFrameRequest);
      if (journeyAnimationFrame != null) cancelAnimationFrame(journeyAnimationFrame);
    }

    function fallback() {
      if (finished) return;
      finished = true;
      clearTimeout(startTimer);
      clearTimeout(endTimer);
      clearTimeout(copyTimer);
      clearTimeout(outgoingPauseTimer);
      journeyVideo.removeEventListener('ended', finish);
      journeyVideo.removeEventListener('timeupdate', onJourneyProgress);
      sceneVideo?.removeEventListener('playing', revealScene);
      stopJourneyFrameWatch();
      journeyVideo.pause();
      journeyVideo.classList.remove('is-visible');
      leaving.classList.remove('is-journey-leaving');
      entering.classList.remove('is-video-entering', 'is-copy-visible', 'is-video-settling');
      primedSceneVideo = null;
      journeyOutgoingVideo = null;
      journeyActive = false;
      traveling = false;
      simpleTo(next);
    }

    function finish() {
      if (finished) return;
      primeScene();
      finished = true;
      clearTimeout(startTimer);
      clearTimeout(endTimer);
      clearTimeout(copyTimer);
      clearTimeout(outgoingPauseTimer);
      journeyVideo.removeEventListener('ended', finish);
      journeyVideo.removeEventListener('timeupdate', onJourneyProgress);
      sceneVideo?.removeEventListener('playing', revealScene);
      stopJourneyFrameWatch();
      entering.classList.add('is-copy-visible', 'is-video-settling');
      journeyVideo.classList.remove('is-visible');
      journeyVideo.pause();
      leaving.classList.remove('is-journey-leaving');
      journeyActive = false;
      primedSceneVideo = null;
      journeyOutgoingVideo = null;
      commitScene(next);
      setTimeout(() => {
        entering.classList.remove('is-video-entering', 'is-copy-visible', 'is-video-settling');
        traveling = false;
        lockedUntil = Date.now() + 260;
      }, 300);
    }

    journeyVideo.addEventListener('ended', finish);
    journeyVideo.addEventListener('timeupdate', onJourneyProgress);
    startTimer = setTimeout(fallback, 8000);
    try { journeyVideo.currentTime = 0; } catch { /* The file is still loading. */ }
    updateJourneyCrop();
    journeyVideo.play().then(() => {
      if (finished) return;
      clearTimeout(startTimer);
      leaving.classList.add('is-journey-leaving');
      journeyVideo.classList.add('is-visible');
      // Keep the live room moving until the travel footage fully covers it.
      outgoingPauseTimer = setTimeout(() => {
        journeyOutgoingVideo = null;
        playCurrent();
      }, 420);
      if (journeyVideo.requestVideoFrameCallback) {
        journeyFrameRequest = journeyVideo.requestVideoFrameCallback(onJourneyFrame);
      } else {
        journeyAnimationFrame = requestAnimationFrame(onJourneyAnimationFrame);
      }
      const duration = journeyVideo.duration;
      copyTimer = setTimeout(() => entering.classList.add('is-copy-visible'), Number.isFinite(duration) ? Math.min(1900, Math.max(520, duration * 180)) : 1800);
      endTimer = setTimeout(finish, Number.isFinite(duration) ? duration * 1000 + 1200 : 14000);
    }).catch(fallback);
  }

  function goTo(index, force = false) {
    if (document.body.classList.contains('is-loading')) return;
    const next = Math.max(0, Math.min(scenes.length - 1, index));
    if (next === active || traveling || (!force && Date.now() < lockedUntil)) return;
    if (!reduceMotion.matches) {
      if ((active === 0 && next === 1) || (active === 1 && next === 2)) journeyTo(next);
      else simpleTo(next);
    } else {
      lockedUntil = Date.now() + (reduceMotion.matches ? 150 : 800);
      commitScene(next);
    }
  }

  addEventListener('wheel', event => {
    if (modalOpen() || event.ctrlKey || event.target?.closest?.('input, textarea')) return;
    const currentScene = scenes[active];
    const canScroll = currentScene.scrollHeight > currentScene.clientHeight + 2;
    if (canScroll && ((event.deltaY > 0 && currentScene.scrollTop + currentScene.clientHeight < currentScene.scrollHeight - 2) || (event.deltaY < 0 && currentScene.scrollTop > 2))) return;
    event.preventDefault();
    if (traveling || Date.now() < lockedUntil) return;
    wheelAmount += event.deltaY;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { wheelAmount = 0; }, 180);
    if (Math.abs(wheelAmount) > 26) {
      goTo(active + Math.sign(wheelAmount));
      wheelAmount = 0;
    }
  }, { passive: false });

  addEventListener('touchstart', event => {
    if (modalOpen() || event.touches.length !== 1 || event.target?.closest?.('input, textarea')) return;
    touchStart = { y: event.touches[0].clientY, x: event.touches[0].clientX, scrollTop: scenes[active].scrollTop };
  }, { passive: true });
  addEventListener('touchend', event => {
    if (!touchStart || event.changedTouches.length !== 1 || modalOpen()) return;
    const dy = touchStart.y - event.changedTouches[0].clientY;
    const dx = touchStart.x - event.changedTouches[0].clientX;
    const didScroll = Math.abs(scenes[active].scrollTop - touchStart.scrollTop) > 4;
    touchStart = null;
    if (!didScroll && Math.abs(dy) > 45 && Math.abs(dy) > Math.abs(dx) * 1.2) goTo(active + Math.sign(dy));
  }, { passive: true });

  addEventListener('keydown', event => {
    if (modalOpen() || event.target?.closest?.('input, textarea, select')) return;
    if (['ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); goTo(active + 1); }
    if (['ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); goTo(active - 1); }
    if (event.key === 'Home') { event.preventDefault(); goTo(0, true); }
    if (event.key === 'End') { event.preventDefault(); goTo(scenes.length - 1, true); }
  });

  navLinks.forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    goTo(Number(link.dataset.go), true);
  }));
  document.querySelector('[data-next]').addEventListener('click', () => goTo(active + 1, true));
  document.querySelectorAll('[data-open-contact]').forEach(button => button.addEventListener('click', () => {
    dialog.showModal();
    playCurrent();
  }));
  document.querySelector('[data-close-contact]').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', playCurrent);
  dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
  document.querySelectorAll('[data-open-offer]').forEach(button => button.addEventListener('click', () => {
    offerDialog.showModal();
    playCurrent();
  }));
  document.querySelector('[data-close-offer]').addEventListener('click', () => offerDialog.close());
  offerDialog.addEventListener('close', playCurrent);
  offerDialog.addEventListener('click', event => { if (event.target === offerDialog) offerDialog.close(); });
  reduceMotion.addEventListener('change', playCurrent);

  if (config.whatsapp) {
    submitButton.innerHTML = 'Написать в WhatsApp <span aria-hidden="true">↗</span>';
    status.textContent = 'После нажатия откроется WhatsApp с готовым сообщением.';
  } else if (config.email) {
    submitButton.innerHTML = 'Отправить по почте <span aria-hidden="true">↗</span>';
    status.textContent = 'После нажатия откроется почтовое приложение.';
  }

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const fields = new FormData(form);
    const message = [
      'Здравствуйте! Хочу заказать шоколадный фонтан.',
      'Имя: ' + fields.get('name'),
      'Контакт: ' + fields.get('contact'),
      'Дата: ' + (fields.get('date') || 'уточняется'),
      'Гостей: ' + (fields.get('guests') || 'уточняется'),
      'Событие: ' + (fields.get('message') || 'уточняется')
    ].join('\n');
    if (config.whatsapp) {
      window.open('https://wa.me/' + String(config.whatsapp).replace(/\D/g, '') + '?text=' + encodeURIComponent(message), '_blank', 'noopener');
      status.textContent = 'Открылся WhatsApp с готовой заявкой.';
    } else if (config.email) {
      location.href = 'mailto:' + encodeURIComponent(config.email) + '?subject=' + encodeURIComponent('Заявка с сайта: шоколадный фонтан') + '&body=' + encodeURIComponent(message);
      status.textContent = 'Открылось почтовое приложение с готовой заявкой.';
    } else {
      try {
        await navigator.clipboard.writeText(message);
        status.textContent = 'Заявка скопирована. Позвоните Анастасии по номеру 8 961 323-77-33.';
      } catch {
        status.textContent = 'Скопируйте заявку: ' + message.replaceAll('\n', ' · ');
      }
    }
  });

  async function startSite() {
    await (window.sitePreloader?.ready || Promise.resolve());
    railProgress.style.height = (100 / scenes.length) + '%';
    Object.values(journeyVideos).forEach(video => {
      if (video.src) return;
      video.src = video.dataset.video;
      video.preload = 'auto';
      video.load();
    });
    const hashIndex = scenes.findIndex(scene => '#' + scene.id === location.hash);
    if (hashIndex > 0) commitScene(hashIndex);
    else {
      prepareVideo(scenes[0]);
      playCurrent();
    }
    const firstVideo = scenes[active].querySelector('video');
    if (firstVideo && !reduceMotion.matches) {
      await Promise.race([
        firstVideo.play().catch(() => {}),
        new Promise(resolve => setTimeout(resolve, 1600))
      ]);
    }
    if (window.sitePreloader) window.sitePreloader.reveal();
    else document.getElementById('site-loader')?.remove();
    document.body.classList.remove('is-loading');
  }

  startSite().catch(error => {
    console.error('Site startup failed:', error);
    if (window.sitePreloader) window.sitePreloader.reveal();
    else document.getElementById('site-loader')?.remove();
    document.body.classList.remove('is-loading');
  });
})();
