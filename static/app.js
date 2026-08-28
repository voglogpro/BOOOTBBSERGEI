"use strict";

const tg = window.Telegram?.WebApp;
const state = {
  user: null,
  circle: null,
  bootstrap: null,
  trades: [],
  calendarMonth: new Date(new Date().getFullYear(), new Date().getMonth(), 1),
  calendarScope: "me",
  statsScope: "me",
  calendarDays: new Map(),
  selectedDay: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const moodEmoji = ["", "😣", "😕", "😐", "🙂", "🔥"];

function todayISO() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

function localDateTimeValue(date = new Date()) {
  const offset = date.getTimezoneOffset();
  return new Date(date.getTime() - offset * 60000).toISOString().slice(0, 16);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[char]);
}

function formatMoney(value, signed = false) {
  const amount = Number(value || 0);
  const sign = signed && amount > 0 ? "+" : "";
  return `${sign}${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 }).format(amount)} $`;
}

function formatDate(value, options = { day: "numeric", month: "long" }) {
  if (!value) return "";
  const source = value.length === 10 ? `${value}T12:00:00` : value;
  return new Intl.DateTimeFormat("ru-RU", options).format(new Date(source));
}

function displayName(user) {
  const full = [user?.first_name, user?.last_name].filter(Boolean).join(" ");
  return full || user?.username || "Трейдер";
}

function haptic(type = "light") {
  try {
    if (tg?.isVersionAtLeast?.("6.1")) tg.HapticFeedback?.impactOccurred(type);
  } catch (_) { /* optional API */ }
}

function toast(message, timeout = 2600) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.remove("hidden");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => element.classList.add("hidden"), timeout);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Telegram-Init-Data", tg?.initData || "");
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || `Ошибка ${response.status}`);
  return payload;
}

function setLoading(loading) {
  $("#loading").classList.toggle("hidden", !loading);
  $("#app").classList.toggle("hidden", loading);
}

function showFatal(error) {
  $("#loading").classList.add("hidden");
  $("#app").classList.add("hidden");
  $("#fatal").classList.remove("hidden");
  $("#fatal-message").textContent = error.message || String(error);
}

function applyTelegramTheme() {
  if (!tg) return;
  try {
    tg.ready();
    tg.expand();
    if (tg.isVersionAtLeast?.("6.1")) {
      tg.setHeaderColor("secondary_bg_color");
      tg.setBackgroundColor("bg_color");
    }
    if (tg.isVersionAtLeast?.("6.2")) tg.enableClosingConfirmation();
  } catch (_) { /* browser preview */ }
}

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;
  state.user = data.user;
  state.circle = data.circle;
  state.trades = data.today_trades || [];
  renderHeader();
  renderToday();
  populateMood(data.today_mood);
  renderTeam();
}

function renderHeader() {
  const name = displayName(state.user).split(" ")[0];
  $("#greeting").textContent = `Привет, ${name}`;
  $("#today-label").textContent = formatDate(todayISO(), { weekday: "long", day: "numeric", month: "long" });
  const avatar = $("#user-avatar");
  avatar.textContent = name.slice(0, 1).toUpperCase();
  if (state.user.photo_url) avatar.style.backgroundImage = `url(${JSON.stringify(state.user.photo_url).slice(1, -1)})`;
}

function stabilityCaption(score) {
  if (score >= 80) return "Решения устойчивы. Сохраняй темп.";
  if (score >= 60) return "Хорошая база. Следи за повторяемостью.";
  if (score >= 35) return "Стабильность растёт вместе с честными записями.";
  return "Начни с честной отметки состояния.";
}

function renderToday() {
  const stats = state.bootstrap.stats || {};
  const pnl = state.trades.reduce((sum, trade) => sum + Number(trade.pnl || 0), 0);
  const planned = state.trades.filter((trade) => Number(trade.plan_followed) === 1).length;
  const planRate = state.trades.length ? Math.round(planned / state.trades.length * 100) : 0;
  $("#stability-score").textContent = stats.stability_score || 0;
  $("#stability-caption").textContent = stabilityCaption(stats.stability_score || 0);
  $("#streak-value").textContent = stats.streak || 0;
  $("#today-trades-count").textContent = state.trades.length;
  $("#today-pnl").textContent = formatMoney(pnl, true);
  $("#today-pnl").className = pnl > 0 ? "positive" : pnl < 0 ? "negative" : "";
  $("#plan-rate").textContent = `${planRate}%`;
  renderTradeList();
}

function populateMood(mood) {
  if (!mood) return;
  const form = $("#mood-form");
  const radio = $(`input[name="mood"][value="${mood.mood}"]`, form);
  if (radio) radio.checked = true;
  for (const key of ["energy", "confidence", "discipline"]) {
    form.elements[key].value = mood[key];
    $(`#${key}-output`).value = mood[key];
  }
  form.elements.emotion.value = mood.emotion || "";
  form.elements.note.value = mood.note || "";
  form.elements.visibility.checked = mood.visibility === "team";
  $("#mood-saved").classList.remove("hidden");
}

function renderTradeList() {
  const container = $("#today-trades");
  if (!state.trades.length) {
    container.innerHTML = '<div class="empty-state">Сделок пока нет. Хороший день может быть и без входа.</div>';
    return;
  }
  container.innerHTML = state.trades.map((trade) => {
    const own = Number(trade.user_id) === Number(state.user.id);
    const pnl = Number(trade.pnl || 0);
    const author = own ? "Вы" : escapeHtml(displayName(trade));
    return `<article class="trade-item" data-trade-id="${trade.id}" data-own="${own}">
      <div class="trade-direction ${trade.direction.toLowerCase()}">${trade.direction === "BUY" ? "↑" : "↓"}</div>
      <div class="trade-main"><strong>${escapeHtml(trade.symbol)} · ${escapeHtml(trade.timeframe || "—")}</strong><small>${author} · ${formatDate(trade.traded_at, { hour: "2-digit", minute: "2-digit" })}${trade.setup ? ` · ${escapeHtml(trade.setup)}` : ""}</small></div>
      <div class="trade-result ${pnl > 0 ? "positive" : pnl < 0 ? "negative" : ""}">${formatMoney(pnl, true)}</div>
    </article>`;
  }).join("");
  $$(".trade-item", container).forEach((item) => item.addEventListener("click", () => {
    if (item.dataset.own !== "true") return toast("Сделку друга можно только просматривать");
    const trade = state.trades.find((candidate) => String(candidate.id) === item.dataset.tradeId);
    if (trade) openTradeDialog(trade);
  }));
}

async function saveMood(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const payload = {
    mood: Number(data.get("mood")), energy: Number(data.get("energy")),
    confidence: Number(data.get("confidence")), discipline: Number(data.get("discipline")),
    emotion: data.get("emotion"), note: data.get("note"),
    visibility: form.elements.visibility.checked ? "team" : "private",
  };
  await api(`/api/moods/${todayISO()}`, { method: "PUT", body: JSON.stringify(payload) });
  $("#mood-saved").classList.remove("hidden");
  haptic("light");
  toast("Состояние сохранено");
  await loadBootstrap();
}

function openTradeDialog(trade = null) {
  const dialog = $("#trade-dialog");
  const form = $("#trade-form");
  form.reset();
  form.elements.traded_at.value = localDateTimeValue();
  form.elements.symbol.value = "XAUUSD";
  form.elements.timeframe.value = "M15";
  form.elements.plan_followed.checked = true;
  form.elements.visibility.checked = true;
  form.elements.trade_id.value = "";
  $("#delete-trade").classList.add("hidden");
  if (trade) {
    const fields = ["symbol", "direction", "timeframe", "setup", "entry_price", "stop_loss", "take_profit", "volume", "risk_amount", "pnl", "r_multiple", "emotion_before", "emotion_after", "mistake", "note", "screenshot_url"];
    form.elements.trade_id.value = trade.id;
    form.elements.traded_at.value = String(trade.traded_at).slice(0, 16);
    for (const field of fields) form.elements[field].value = trade[field] ?? "";
    form.elements.plan_followed.checked = Boolean(Number(trade.plan_followed));
    form.elements.visibility.checked = trade.visibility === "team";
    $("#delete-trade").classList.remove("hidden");
  }
  dialog.showModal();
}

function tradePayload(form) {
  const data = new FormData(form);
  const optionalNumbers = ["entry_price", "stop_loss", "take_profit", "volume", "risk_amount", "r_multiple"];
  const payload = Object.fromEntries(data.entries());
  delete payload.trade_id;
  for (const key of optionalNumbers) payload[key] = payload[key] === "" ? null : Number(payload[key]);
  payload.pnl = Number(payload.pnl);
  payload.plan_followed = form.elements.plan_followed.checked;
  payload.visibility = form.elements.visibility.checked ? "team" : "private";
  return payload;
}

async function saveTrade(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const id = form.elements.trade_id.value;
  const path = id ? `/api/trades/${id}` : "/api/trades";
  await api(path, { method: id ? "PUT" : "POST", body: JSON.stringify(tradePayload(form)) });
  $("#trade-dialog").close();
  haptic("medium");
  toast(id ? "Сделка обновлена" : "Сделка добавлена");
  await loadBootstrap();
  if ($("#view-calendar").classList.contains("active")) await loadCalendar();
}

async function confirmDelete() {
  const id = $("#trade-form").elements.trade_id.value;
  if (!id) return;
  const confirmed = await new Promise((resolve) => {
    if (tg?.showConfirm) tg.showConfirm("Удалить эту сделку из журнала?", resolve);
    else resolve(window.confirm("Удалить эту сделку из журнала?"));
  });
  if (!confirmed) return;
  await api(`/api/trades/${id}`, { method: "DELETE" });
  $("#trade-dialog").close();
  toast("Сделка удалена");
  await loadBootstrap();
}

function switchView(name) {
  $$(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".bottom-nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  window.scrollTo({ top: 0, behavior: "smooth" });
  if (name === "calendar") loadCalendar().catch(showRequestError);
  if (name === "stats") loadStats().catch(showRequestError);
  if (name === "team") renderTeam();
}

async function loadCalendar() {
  const month = `${state.calendarMonth.getFullYear()}-${String(state.calendarMonth.getMonth() + 1).padStart(2, "0")}`;
  const data = await api(`/api/calendar?month=${month}&scope=${state.calendarScope}`);
  state.calendarDays = new Map(data.days.map((item) => [item.day, item]));
  renderCalendar();
}

function renderCalendar() {
  const year = state.calendarMonth.getFullYear();
  const month = state.calendarMonth.getMonth();
  $("#calendar-title").textContent = new Intl.DateTimeFormat("ru-RU", { month: "long", year: "numeric" }).format(state.calendarMonth);
  const firstWeekday = (new Date(year, month, 1).getDay() + 6) % 7;
  const daysCount = new Date(year, month + 1, 0).getDate();
  const cells = [];
  for (let i = 0; i < firstWeekday; i += 1) cells.push('<div class="calendar-day" aria-hidden="true"></div>');
  for (let day = 1; day <= daysCount; day += 1) {
    const key = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    const info = state.calendarDays.get(key);
    const classes = ["calendar-day", info ? "has-data" : "", key === todayISO() ? "today" : "", key === state.selectedDay ? "selected" : ""].filter(Boolean).join(" ");
    const pnl = info ? Number(info.pnl || 0) : 0;
    cells.push(`<button class="${classes}" data-day="${key}" type="button"><span>${day}</span>${info?.mood ? `<span class="day-mood">${moodEmoji[Math.round(info.mood)]}</span>` : "<span></span>"}${info?.trades ? `<span class="day-pnl ${pnl > 0 ? "positive" : pnl < 0 ? "negative" : ""}">${pnl > 0 ? "+" : ""}${Math.round(pnl)}$</span>` : "<span></span>"}</button>`);
  }
  $("#calendar-grid").innerHTML = cells.join("");
  $$(".calendar-day[data-day]").forEach((button) => button.addEventListener("click", () => selectDay(button.dataset.day)));
}

async function selectDay(day) {
  state.selectedDay = day;
  renderCalendar();
  const info = state.calendarDays.get(day) || { trades: 0, pnl: 0 };
  const payload = await api(`/api/trades?from=${day}&to=${day}&scope=${state.calendarScope}`);
  const trades = payload.trades || [];
  const tradeRows = trades.length ? trades.map((trade) => `<div class="trade-item"><div class="trade-direction ${trade.direction.toLowerCase()}">${trade.direction === "BUY" ? "↑" : "↓"}</div><div class="trade-main"><strong>${escapeHtml(trade.symbol)}</strong><small>${escapeHtml(displayName(trade))} · ${escapeHtml(trade.setup || "Без описания")}</small></div><div class="trade-result ${Number(trade.pnl) > 0 ? "positive" : Number(trade.pnl) < 0 ? "negative" : ""}">${formatMoney(trade.pnl, true)}</div></div>`).join("") : '<p class="muted">В этот день сделки не записаны.</p>';
  $("#day-details").innerHTML = `<h3>${formatDate(day, { day: "numeric", month: "long", weekday: "long" })}</h3><div class="day-summary"><span>Сделок<strong>${info.trades || 0}</strong></span><span>Итог<strong class="${Number(info.pnl) > 0 ? "positive" : Number(info.pnl) < 0 ? "negative" : ""}">${formatMoney(info.pnl, true)}</strong></span>${info.mood ? `<span>Настроение<strong>${moodEmoji[Math.round(info.mood)]}</strong></span>` : ""}</div><div class="trade-list day-trades">${tradeRows}</div>`;
}

async function loadStats() {
  const days = $("#stats-period").value;
  const data = await api(`/api/stats?days=${days}&scope=${state.statsScope}`);
  const stats = data.stats;
  $("#stats-pnl").textContent = formatMoney(stats.pnl, true);
  $("#stats-pnl").className = Number(stats.pnl) > 0 ? "positive" : Number(stats.pnl) < 0 ? "negative" : "";
  $("#stats-trades").textContent = `${stats.trades || 0} сделок`;
  $("#stats-winrate").textContent = `${stats.win_rate || 0}%`;
  $("#stats-wl").textContent = `${stats.wins || 0} / ${stats.losses || 0}`;
  $("#stats-plan").textContent = `${stats.plan_rate || 0}%`;
  $("#stats-r").textContent = stats.avg_r ?? "—";
  $("#stats-stability").textContent = stats.stability_score || 0;
  $("#stats-discipline").textContent = `${stats.avg_discipline || 0}/5`;
  $("#stability-bar").style.width = `${Math.min(stats.stability_score || 0, 100)}%`;
  $("#discipline-bar").style.width = `${Math.min(Number(stats.avg_discipline || 0) / 5 * 100, 100)}%`;
  renderInsight(stats);
}

function renderInsight(stats) {
  let title = "Сначала собери данные";
  let copy = "После нескольких записей здесь появится подсказка по стабильности.";
  if ((stats.trades || 0) >= 3) {
    if ((stats.plan_rate || 0) < 70) { title = "Результат уступает процессу"; copy = "Сделок по плану меньше 70%. Снизь частоту и запиши условие отмены входа."; }
    else if ((stats.avg_discipline || 0) < 3.5) { title = "Проверь состояние до входа"; copy = "Дисциплина проседает. Перед каждой сделкой оцени эмоцию и готовность принять стоп."; }
    else if ((stats.stability_score || 0) >= 75) { title = "Стабильный процесс"; copy = "Ты регулярно ведёшь журнал и следуешь плану. Не увеличивай риск из-за короткой серии побед."; }
    else { title = "Продолжай одинаковый ритуал"; copy = "Записывай состояние, причину входа и результат. Через несколько недель закономерности станут заметнее."; }
  }
  $("#stats-insight").textContent = title;
  $("#stats-insight-copy").textContent = copy;
}

function renderTeam() {
  const active = Boolean(state.circle);
  $("#team-empty").classList.toggle("hidden", active);
  $("#team-active").classList.toggle("hidden", !active);
  if (!active) return;
  $("#invite-code").textContent = state.circle.invite_code;
  $("#team-members").innerHTML = (state.circle.members || []).map((member) => {
    const name = displayName(member);
    return `<article class="member"><div class="avatar">${escapeHtml(name.slice(0, 1).toUpperCase())}</div><div><strong>${escapeHtml(name)}</strong><small>${Number(member.id) === Number(state.user.id) ? "Это вы" : "Напарник"}</small></div></article>`;
  }).join("");
}

async function createTeam(event) {
  event.preventDefault();
  const name = new FormData(event.currentTarget).get("name");
  const data = await api("/api/circles", { method: "POST", body: JSON.stringify({ name }) });
  state.circle = data.circle; renderTeam(); haptic("medium"); toast("Команда создана");
}

async function joinTeam(event) {
  event.preventDefault();
  const invite_code = new FormData(event.currentTarget).get("invite_code");
  const data = await api("/api/circles/join", { method: "POST", body: JSON.stringify({ invite_code }) });
  state.circle = data.circle; renderTeam(); haptic("medium"); toast("Вы присоединились к команде");
}

async function leaveTeam() {
  const confirmed = window.confirm("Покинуть команду? Ваши записи не удалятся.");
  if (!confirmed) return;
  await api("/api/circles/leave", { method: "POST", body: "{}" });
  state.circle = null; renderTeam(); toast("Вы покинули команду");
}

function showRequestError(error) {
  console.error(error);
  toast(error.message || "Не удалось выполнить действие", 4200);
  try {
    if (tg?.isVersionAtLeast?.("6.1")) tg.HapticFeedback?.notificationOccurred("error");
  } catch (_) { /* optional */ }
}

function bindEvents() {
  $$(".bottom-nav button").forEach((button) => button.addEventListener("click", () => switchView(button.dataset.view)));
  $("#mood-form").addEventListener("submit", (event) => saveMood(event).catch(showRequestError));
  for (const key of ["energy", "confidence", "discipline"]) {
    $(`#${key}`).addEventListener("input", (event) => { $(`#${key}-output`).value = event.target.value; });
  }
  $("#open-trade-button").addEventListener("click", () => openTradeDialog());
  $("#close-trade-dialog").addEventListener("click", () => $("#trade-dialog").close());
  $("#trade-form").addEventListener("submit", (event) => saveTrade(event).catch(showRequestError));
  $("#delete-trade").addEventListener("click", () => confirmDelete().catch(showRequestError));
  $("#calendar-prev").addEventListener("click", () => { state.calendarMonth.setMonth(state.calendarMonth.getMonth() - 1); state.selectedDay = null; loadCalendar().catch(showRequestError); });
  $("#calendar-next").addEventListener("click", () => { state.calendarMonth.setMonth(state.calendarMonth.getMonth() + 1); state.selectedDay = null; loadCalendar().catch(showRequestError); });
  $$('[data-scope-control="calendar"] button').forEach((button) => button.addEventListener("click", () => {
    state.calendarScope = button.dataset.scope; $$('[data-scope-control="calendar"] button').forEach((item) => item.classList.toggle("active", item === button)); loadCalendar().catch(showRequestError);
  }));
  $$('[data-scope-control="stats"] button').forEach((button) => button.addEventListener("click", () => {
    state.statsScope = button.dataset.scope; $$('[data-scope-control="stats"] button').forEach((item) => item.classList.toggle("active", item === button)); loadStats().catch(showRequestError);
  }));
  $("#stats-period").addEventListener("change", () => loadStats().catch(showRequestError));
  $("#create-team-form").addEventListener("submit", (event) => createTeam(event).catch(showRequestError));
  $("#join-team-form").addEventListener("submit", (event) => joinTeam(event).catch(showRequestError));
  $("#copy-invite").addEventListener("click", async () => { await navigator.clipboard.writeText(state.circle.invite_code); haptic(); toast("Код скопирован"); });
  $("#leave-team").addEventListener("click", () => leaveTeam().catch(showRequestError));
  $("#retry-button").addEventListener("click", () => window.location.reload());
}

async function start() {
  applyTelegramTheme();
  bindEvents();
  await loadBootstrap();
  setLoading(false);
}

start().catch(showFatal);
