(() => {
  const config = window.SITE_CONFIG || {};
  const scenes = [...document.querySelectorAll('.scene')];
  const dots = [...document.querySelectorAll('.scene-dots button')];
  const navLinks = [...document.querySelectorAll('[data-go]')];
  const currentLabel = document.getElementById('current-index');
  const railProgress = document.getElementById('rail-progress');
  const dialog = document.getElementById('contact-dialog');
  const offerDialog = document.getElementById('offer-dialog');
  const cityDialog = document.getElementById('city-dialog');
  const formTemplate = document.getElementById('lead-form-template');
  const nextButton = document.querySelector('.scene-next');
  const nextLabel = document.getElementById('scene-next-label');
  const telegramUser = window.Telegram?.WebApp?.initDataUnsafe?.user;
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

  function setAll(selector, value) {
    if (value) document.querySelectorAll(selector).forEach(node => { node.textContent = value; });
  }

  // Each form slot gets its own copy of the lead form before config values are applied.
  document.querySelectorAll('[data-lead-form-slot]').forEach(slot => slot.append(formTemplate.content.cloneNode(true)));

  setAll('[data-price]', config.price);
  document.getElementById('year').textContent = new Date().getFullYear();

  // City: the address (/krasnodar, /rostov) wins, then the visitor's last choice.
  const cities = config.cities || {};
  const cityKeys = Object.keys(cities);
  const storage = {
    get() { try { return localStorage.getItem('fountain-city'); } catch { return null; } },
    set(value) { try { localStorage.setItem('fountain-city', value); } catch { /* Private mode. */ } }
  };
  const pathCity = document.documentElement.dataset.city || location.pathname.replace(/^\/+|\/+$/g, '').toLowerCase();
  let cityKey = cities[pathCity] ? pathCity : '';
  let city = {};
  let phoneHref = '';
  let whatsappNumber = '';
  let vkHref = '';

  function showLinks(selector, href) {
    document.querySelectorAll(selector).forEach(link => {
      link.hidden = !href;
      if (href) link.href = href;
    });
  }

  // "Нам доверяют": organisations that ordered from this city's master.
  function renderClients(clients) {
    document.querySelectorAll('[data-clients]').forEach(block => {
      const list = block.querySelector('ul');
      list.replaceChildren(...clients.map(client => {
        const item = document.createElement('li');
        const name = document.createElement('b');
        name.textContent = client.name;
        item.append(name);
        if (client.note) {
          const note = document.createElement('small');
          note.textContent = client.note;
          item.append(note);
        }
        return item;
      }));
      block.hidden = !clients.length;
    });
    document.documentElement.classList.toggle('has-clients', clients.length > 0);
  }

  function applyCity(key, { remember = true, updateUrl = true } = {}) {
    if (!cities[key]) return;
    cityKey = key;
    city = cities[key];
    document.documentElement.dataset.city = key;
    setAll('[data-city-name]', city.name);
    setAll('[data-city-prep]', city.prep);
    setAll('[data-city-gen]', city.gen);
    setAll('[data-city-in]', city.in);
    setAll('[data-owner]', city.owner);
    setAll('[data-owner-dative]', city.ownerDative);
    setAll('[data-owner-instrumental]', city.ownerInstrumental);
    setAll('[data-owner-full]', city.ownerFull);
    setAll('[data-phone-text]', city.phoneText);
    phoneHref = city.phone ? 'tel:' + String(city.phone).replace(/[^\d+]/g, '') : '';
    whatsappNumber = String(city.whatsapp || '').replace(/\D/g, '');
    vkHref = city.vkChat || city.vk || '';
    document.querySelectorAll('[data-phone]').forEach(link => { if (phoneHref) link.href = phoneHref; });
    showLinks('[data-whatsapp]', whatsappNumber ? 'https://wa.me/' + whatsappNumber : '');
    showLinks('[data-vk]', vkHref);
    document.querySelectorAll('[data-channel="whatsapp"]').forEach(button => { button.hidden = !whatsappNumber; });
    document.querySelectorAll('[data-channel="vk"]').forEach(button => { button.hidden = !vkHref; });
    document.querySelectorAll('[data-city-option]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.cityOption === key)));
    document.title = `${config.brand || 'Шоколадная фабрика'} — шоколадный фонтан на праздник ${city.in}`;
    renderClients(city.clients || []);
    document.dispatchEvent(new Event('citychange'));
    if (remember) storage.set(key);
    if (updateUrl && location.pathname !== '/' + key) history.replaceState(null, '', '/' + key + location.search + location.hash);
  }

  const cityOptions = document.querySelector('[data-city-options]');
  cityKeys.forEach(key => {
    const item = cities[key];
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'city-option';
    button.dataset.cityOption = key;
    button.innerHTML = '<svg aria-hidden="true"><use href="#i-route" /></svg><span><b></b><small></small></span><svg class="city-option-arrow" aria-hidden="true"><use href="#i-arrow" /></svg>';
    button.querySelector('b').textContent = item.name;
    button.querySelector('small').textContent = `Мастер — ${item.owner}`;
    const phoneLine = document.createElement('i');
    phoneLine.textContent = item.phoneText;
    button.querySelector('small').append(' · ', phoneLine);
    button.addEventListener('click', () => {
      applyCity(key);
      cityDialog.close();
    });
    cityOptions?.append(button);
  });

  function openCityDialog() {
    // Without a city there are no contacts to show, so the first choice cannot be skipped.
    cityDialog.classList.toggle('is-required', !cityKey);
    if (!cityDialog.open) cityDialog.showModal();
    playCurrent();
  }
  cityDialog.addEventListener('cancel', event => { if (!cityKey) event.preventDefault(); });
  cityDialog.addEventListener('click', event => { if (event.target === cityDialog && cityKey) cityDialog.close(); });
  cityDialog.addEventListener('close', playCurrent);
  document.querySelector('[data-close-city]').addEventListener('click', () => { if (cityKey) cityDialog.close(); });
  document.querySelectorAll('[data-open-city]').forEach(button => button.addEventListener('click', openCityDialog));

  if (!cityKey && cities[storage.get()]) cityKey = storage.get();
  // Fill the page with the first city until the visitor chooses, so nothing reads empty behind the chooser.
  if (cityKey) applyCity(cityKey, { remember: Boolean(cities[pathCity]) });
  else if (cityKeys.length) {
    applyCity(cityKeys[0], { remember: false, updateUrl: false });
    cityKey = '';
    document.querySelectorAll('[data-city-option]').forEach(button => button.setAttribute('aria-pressed', 'false'));
  }

  if (config.metrikaId) {
    const id = Number(config.metrikaId);
    window.ym = window.ym || function () { (window.ym.a = window.ym.a || []).push(arguments); };
    window.ym.l = Date.now();
    const tag = document.createElement('script');
    tag.async = true;
    tag.src = 'https://mc.yandex.ru/metrika/tag.js';
    document.head.append(tag);
    window.ym(id, 'init', { clickmap: true, trackLinks: true, accurateTrackBounce: true, webvisor: true });
  }
  function goal(name) {
    if (config.metrikaId && window.ym) window.ym(Number(config.metrikaId), 'reachGoal', name);
  }
  document.querySelectorAll('[data-goal]').forEach(link => link.addEventListener('click', () => goal(link.dataset.goal)));

  const modalOpen = () => dialog.open || offerDialog.open || cityDialog.open;

  function prepareVideo(scene) {
    scene.querySelectorAll('video[data-video]').forEach(video => {
      if (video.dataset.loaded || reduceMotion.matches) return;
      video.dataset.loaded = 'true';
      video.preload = 'auto';
      video.src = video.dataset.video;
      video.addEventListener('loadeddata', () => {
        video.classList.add('is-ready');
        video.dataset.ready = 'true';
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
    const upcoming = scenes[active + 1];
    nextButton.hidden = !upcoming;
    if (upcoming) nextLabel.textContent = upcoming.getAttribute('aria-label');
    document.body.dataset.scene = scenes[active].id;
    railProgress.style.height = ((active + 1) / scenes.length * 100) + '%';
    dots.forEach((dot, index) => {
      dot.classList.toggle('is-current', index === active);
      dot.setAttribute('aria-current', index === active ? 'step' : 'false');
    });
    navLinks.forEach(link => link.classList.toggle('is-current', Number(link.dataset.go) === active));
    history.replaceState(null, '', location.pathname + location.search + '#' + scenes[active].id);
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
    const leaving = scenes[active];
    const entering = scenes[next];
    const sceneVideo = entering.querySelector('video');
    // Travel through the filmed room only when both clips are already in memory.
    const isReady = video => video?.dataset.ready === 'true' && !video.error;
    if (!isReady(journeyVideo) || !isReady(sceneVideo)) { simpleTo(next); return; }
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
  document.querySelectorAll('[data-next]').forEach(button => button.addEventListener('click', () => goTo(active + 1, true)));
  document.querySelectorAll('[data-open-contact]').forEach(button => button.addEventListener('click', () => {
    if (offerDialog.open) offerDialog.close();
    dialog.showModal();
    goal('lead_form_open');
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

  function formatPhone(value) {
    let digits = value.replace(/\D/g, '');
    if (!digits) return '';
    if (digits[0] === '8') digits = '7' + digits.slice(1);
    if (digits[0] === '9') digits = '7' + digits;
    if (digits[0] !== '7') return '+' + digits.slice(0, 15);
    digits = digits.slice(0, 11);
    const parts = [digits.slice(1, 4), digits.slice(4, 7), digits.slice(7, 9), digits.slice(9, 11)];
    let text = '+7';
    if (parts[0]) text += ' (' + parts[0];
    if (parts[0].length === 3) text += ')';
    if (parts[1]) text += ' ' + parts[1];
    if (parts[2]) text += '-' + parts[2];
    if (parts[3]) text += '-' + parts[3];
    return text;
  }

  function leadMessage(lead, { preview = false } = {}) {
    const name = lead.name || (preview ? '…' : '');
    const lines = [
      `Здравствуйте${city.owner ? ', ' + city.owner : ''}! Хочу заказать шоколадный фонтан.`,
      '',
      'Праздник: ' + (lead.event || 'уточню'),
      'Город: ' + (city.name || 'уточню'),
      'Дата: ' + (lead.date || 'уточню'),
      lead.time && 'Время: ' + lead.time,
      lead.guests && 'Гостей: ' + lead.guests,
      lead.place && 'Место: ' + lead.place,
      '',
      'Меня зовут ' + name + (lead.phone ? ', телефон ' + lead.phone : '') + '.'
    ];
    return lines.filter(line => line !== false && line !== undefined && line !== null).join('\n').replace(/\n{3,}/g, '\n\n');
  }

  function readLead(form, channel) {
    const data = new FormData(form);
    return {
      name: String(data.get('name')).trim(),
      phone: String(data.get('phone')).trim(),
      date: data.get('date') ? new Date(data.get('date') + 'T12:00:00').toLocaleDateString('ru-RU', { day: 'numeric', month: 'long', year: 'numeric' }).replace(/\s*г\.$/, '') : '',
      time: String(data.get('time') || ''),
      guests: String(data.get('guests') || ''),
      place: String(data.get('place') || '').trim(),
      event: String(data.get('event') || ''),
      company: String(data.get('company') || ''),
      city: cityKey,
      channel,
      telegram: telegramUser?.username ? '@' + telegramUser.username : '',
      page: location.href
    };
  }

  function postLead(lead) {
    return fetch('/api/lead', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(lead),
      keepalive: true
    });
  }

  // Clipboard API first; the textarea fallback covers older in-app browsers.
  async function copyText(text, container) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* Fall back below. */ }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:0;left:0;width:1px;height:1px;opacity:0';
    container.append(area);
    area.select();
    let copied = false;
    try { copied = document.execCommand('copy'); } catch { copied = false; }
    area.remove();
    return copied;
  }

  function selectText(node) {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }

  // Telegram Mini App blocks window.open; it has its own way to open outside links.
  function openExternal(url) {
    const app = window.Telegram?.WebApp;
    if (app?.initData && app.openLink) {
      try { app.openLink(url); return; } catch { /* Fall back to the browser. */ }
    }
    const opened = window.open(url, '_blank', 'noopener');
    if (!opened) location.href = url;
  }

  // Without lead delivery set up on the server a site request would reach nobody,
  // so the form then sends the assembled message straight to the master instead.
  const siteLeads = document.documentElement.dataset.leads !== 'off';
  if (!siteLeads) document.querySelectorAll('.lead-form').forEach(form => form.classList.add('is-messenger-only'));

  const today = new Date();
  const minDate = new Date(today.getTime() - today.getTimezoneOffset() * 60000).toISOString().slice(0, 10);

  document.querySelectorAll('.lead-form').forEach(form => {
    const status = form.querySelector('.form-status');
    const submit = form.querySelector('button[type=submit]');
    const phone = form.elements.phone;
    const fields = form.querySelector('.lead-fields');
    const success = form.querySelector('.lead-success');
    form.elements.date.min = minDate;
    if (telegramUser?.first_name) form.elements.name.value = telegramUser.first_name;

    phone.addEventListener('input', () => {
      const formatted = formatPhone(phone.value);
      if (formatted !== phone.value) phone.value = formatted;
      phone.setCustomValidity('');
    });

    // The visitor sees the exact message the master will receive as it is being built.
    const bubble = form.querySelector('.msg-bubble');
    const updatePreview = () => { bubble.textContent = leadMessage(readLead(form, 'preview'), { preview: true }); };
    form.addEventListener('input', updatePreview);
    form.addEventListener('change', updatePreview);
    document.addEventListener('citychange', updatePreview);
    updatePreview();

    function setStatus(html, isError = false) {
      status.innerHTML = html;
      status.classList.toggle('is-error', isError);
    }

    function showSuccess(lead, delivered) {
      form.querySelector('.lead-success-text').textContent = delivered
        ? `Спасибо, ${lead.name}! ${city.owner || 'Мы'} скоро перезвонит на номер ${lead.phone}, чтобы уточнить детали и закрепить дату.`
        : `Спасибо, ${lead.name}! Заявка сохранена. Чтобы закрепить дату быстрее, позвоните ${city.ownerDative || 'нам'} прямо сейчас.`;
      fields.hidden = true;
      success.hidden = false;
      success.querySelector('a')?.focus?.({ preventScroll: true });
      try { window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred('success'); } catch { /* Outside Telegram. */ }
    }

    function showFallback(lead) {
      const message = encodeURIComponent(leadMessage(lead));
      const links = [];
      if (whatsappNumber) links.push(`<a href="https://wa.me/${whatsappNumber}?text=${message}" target="_blank" rel="noopener">WhatsApp</a>`);
      if (vkHref) links.push(`<a href="${vkHref}" target="_blank" rel="noopener">ВКонтакте</a>`);
      if (phoneHref) links.push(`<a href="${phoneHref}">позвоните ${city.phoneText || ''}</a>`);
      setStatus('Не удалось отправить заявку — проверьте интернет и попробуйте ещё раз' + (links.length ? ' или свяжитесь напрямую: ' + links.join(', ') + '.' : '.'), true);
    }

    function needCity() {
      if (cityKey) return false;
      openCityDialog();
      return true;
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      setStatus('');
      if (needCity()) return;
      phone.setCustomValidity(phone.value.replace(/\D/g, '').length < 11 ? 'Для заявки на сайте укажите телефон — или отправьте сообщение во ВКонтакте' : '');
      if (!form.reportValidity()) return;
      const lead = readLead(form, 'site');
      submit.disabled = true;
      submit.classList.add('is-sending');
      try {
        const response = await postLead(lead);
        const result = await response.json().catch(() => ({}));
        if (!response.ok || !result.ok) throw new Error('Lead rejected: ' + response.status);
        goal('lead');
        showSuccess(lead, result.delivered);
      } catch (error) {
        console.error(error);
        showFallback(lead);
      } finally {
        submit.disabled = false;
        submit.classList.remove('is-sending');
      }
    });

    // Messenger buttons send the same assembled message straight to the city's master.
    form.querySelectorAll('[data-channel]').forEach(button => button.addEventListener('click', () => {
      setStatus('');
      if (needCity()) return;
      phone.setCustomValidity('');
      if (!form.elements.name.reportValidity()) return;
      const channel = button.dataset.channel;
      const lead = readLead(form, channel);
      const message = leadMessage(lead);
      if (channel === 'whatsapp') {
        openExternal(`https://wa.me/${whatsappNumber}?text=${encodeURIComponent(message)}`);
        setStatus(`<b>✓ Открываем WhatsApp.</b> Сообщение для ${city.ownerDative || 'мастера'} уже вставлено — осталось нажать «Отправить».`);
        goal(channel);
      } else {
        // A VK link cannot carry text: copy it, say so, and open the chat on the next tap.
        copyText(message, form).then(copied => {
          vkStep.querySelector('.vk-step-title span:last-child').textContent = copied ? 'Сообщение скопировано' : 'Скопируйте сообщение';
          if (!copied) selectText(form.querySelector('.msg-bubble'));
          form.classList.add('is-vk-step');
          vkStep.hidden = false;
          vkStep.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        });
      }
      postLead(lead).catch(() => {});
    }));

    const vkStep = form.querySelector('.vk-step');
    vkStep.querySelector('[data-open-vk]').addEventListener('click', () => {
      openExternal(vkHref);
      goal('vk');
    });
    vkStep.querySelector('[data-vk-back]').addEventListener('click', () => {
      vkStep.hidden = true;
      form.classList.remove('is-vk-step');
    });
    form.addEventListener('input', () => {
      if (vkStep.hidden) return;
      vkStep.hidden = true;
      form.classList.remove('is-vk-step');
    });
  });

  async function startSite() {
    await (window.sitePreloader?.ready || Promise.resolve());
    railProgress.style.height = (100 / scenes.length) + '%';
    document.getElementById('rail-total').textContent = String(scenes.length).padStart(2, '0');
    document.body.dataset.scene = scenes[active].id;
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
        new Promise(resolve => setTimeout(resolve, 500))
      ]);
    }
    if (window.sitePreloader) window.sitePreloader.reveal();
    else document.getElementById('site-loader')?.remove();
    document.body.classList.remove('is-loading');
    if (!cityKey && cityKeys.length) setTimeout(openCityDialog, 450);
  }

  startSite().catch(error => {
    console.error('Site startup failed:', error);
    if (window.sitePreloader) window.sitePreloader.reveal();
    else document.getElementById('site-loader')?.remove();
    document.body.classList.remove('is-loading');
  });
})();
