(() => {
  "use strict";

  const API = Object.freeze({
    status: "/api/telegram/auth/status",
    phone: "/api/telegram/auth/phone",
    code: "/api/telegram/auth/code",
    password: "/api/telegram/auth/password",
    cancel: "/api/telegram/auth/cancel",
  });

  const KNOWN_STATES = new Set([
    "unauthorized",
    "phone_required",
    "code_required",
    "password_required",
    "authorized",
    "locked",
  ]);

  const webApp = window.Telegram && window.Telegram.WebApp;
  const initData = webApp && typeof webApp.initData === "string"
    ? webApp.initData
    : "";

  let flowId = null;
  let replaceExisting = false;
  let busy = false;
  let lockTimer = null;

  const app = document.getElementById("app");
  const statusDot = document.getElementById("status-dot");
  const statusTitle = document.getElementById("status-title");
  const statusMessage = document.getElementById("status-message");
  const readerStatus = document.getElementById("reader-status");
  const sessionStatus = document.getElementById("session-status");
  const refreshButton = document.getElementById("refresh-button");
  const successRefreshButton = document.getElementById("success-refresh-button");
  const reauthorizeButton = document.getElementById("reauthorize-button");
  const lockedRefreshButton = document.getElementById("locked-refresh-button");
  const lockedMessage = document.getElementById("locked-message");
  const lockCountdown = document.getElementById("lock-countdown");
  const errorBox = document.getElementById("error-box");
  const errorMessage = document.getElementById("error-message");

  const panels = {
    loading: document.getElementById("loading-panel"),
    phone: document.getElementById("phone-panel"),
    code: document.getElementById("code-panel"),
    password: document.getElementById("password-panel"),
    success: document.getElementById("success-panel"),
    locked: document.getElementById("locked-panel"),
    telegramRequired: document.getElementById("telegram-required-panel"),
  };

  const phoneForm = document.getElementById("phone-form");
  const codeForm = document.getElementById("code-form");
  const passwordForm = document.getElementById("password-form");
  const phoneInput = document.getElementById("phone-input");
  const codeInput = document.getElementById("code-input");
  const passwordInput = document.getElementById("password-input");
  const passwordToggle = document.getElementById("password-toggle");
  const controls = Array.from(document.querySelectorAll("button, input"));

  class ApiError extends Error {
    constructor(message, status = 0, retryAfter = 0) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.retryAfter = retryAfter;
    }
  }

  function setBusy(value) {
    busy = Boolean(value);
    app.setAttribute("aria-busy", String(busy));
    document.body.dataset.busy = String(busy);
    controls.forEach((control) => {
      control.disabled = busy;
    });
  }

  function showPanel(name) {
    Object.entries(panels).forEach(([key, panel]) => {
      panel.hidden = key !== name;
    });
  }

  function clearError() {
    errorMessage.textContent = "";
    errorBox.hidden = true;
  }

  function showError(message) {
    errorMessage.textContent = message;
    errorBox.hidden = false;
  }

  function setStatus({ tone, title, message, reader, session }) {
    statusDot.className = `status-dot status-dot--${tone}`;
    statusTitle.textContent = title;
    statusMessage.textContent = message;
    readerStatus.textContent = reader;
    sessionStatus.textContent = session;
  }

  function normalizeResponse(value) {
    if (!value || typeof value !== "object" || !KNOWN_STATES.has(value.state)) {
      throw new ApiError("Сервер вернул неизвестное состояние. Обновите Mini App.");
    }
    return {
      state: value.state,
      flowId: typeof value.flow_id === "string" && value.flow_id.length <= 256
        ? value.flow_id
        : null,
      message: typeof value.message === "string"
        ? value.message.slice(0, 500)
        : "",
      retryAfter: Number.isFinite(Number(value.retry_after))
        ? Math.max(0, Math.min(86400, Math.floor(Number(value.retry_after))))
        : 0,
    };
  }

  async function apiRequest(path, { method = "GET", body } = {}) {
    if (!initData) {
      throw new ApiError("Mini App не получило данные авторизации Telegram.", 401);
    }

    const headers = {
      Accept: "application/json",
      Authorization: `tma ${initData}`,
    };
    const options = {
      method,
      headers,
      cache: "no-store",
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
      mode: "same-origin",
    };

    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(path, options);
    } catch (_error) {
      throw new ApiError("Нет связи с сервером. Проверьте интернет и повторите попытку.");
    }

    let payload = null;
    const contentType = response.headers.get("content-type") || "";
    if (contentType.includes("application/json")) {
      try {
        payload = await response.json();
      } catch (_error) {
        payload = null;
      }
    }

    if (!response.ok) {
      const safeMessage = payload && typeof payload.message === "string"
        ? payload.message.slice(0, 500)
        : "Сервер отклонил запрос.";
      const retryAfter = payload && Number.isFinite(Number(payload.retry_after))
        ? Math.max(0, Math.floor(Number(payload.retry_after)))
        : 0;
      throw new ApiError(safeMessage, response.status, retryAfter);
    }

    return normalizeResponse(payload);
  }

  function stopLockTimer() {
    if (lockTimer !== null) {
      window.clearInterval(lockTimer);
      lockTimer = null;
    }
  }

  function startLockTimer(seconds) {
    stopLockTimer();
    const finishAt = Date.now() + Math.max(0, seconds) * 1000;

    const update = () => {
      const remaining = Math.max(0, Math.ceil((finishAt - Date.now()) / 1000));
      if (remaining > 0) {
        lockCountdown.textContent = `Повторная проверка через ${remaining} сек.`;
        lockedRefreshButton.disabled = true;
        refreshButton.disabled = true;
        return;
      }
      lockCountdown.textContent = "Можно повторить проверку.";
      lockedRefreshButton.disabled = busy;
      refreshButton.disabled = busy;
      stopLockTimer();
    };

    update();
    if (seconds > 0) {
      lockTimer = window.setInterval(update, 1000);
    }
  }

  function applyState(payload) {
    clearError();
    stopLockTimer();

    if (payload.flowId) {
      flowId = payload.flowId;
    }

    switch (payload.state) {
      case "unauthorized":
      case "phone_required":
        flowId = null;
        replaceExisting = false;
        setStatus({
          tone: "warning",
          title: "Reader не подключён",
          message: payload.message || "Авторизуйте отдельный Telegram-аккаунт для чтения публичных групп.",
          reader: "Ожидает вход",
          session: "Не создана",
        });
        showPanel("phone");
        phoneInput.focus();
        break;

      case "code_required":
        if (!flowId) {
          throw new ApiError("Сервер не вернул идентификатор входа. Начните авторизацию заново.");
        }
        setStatus({
          tone: "warning",
          title: "Ожидаем код",
          message: payload.message || "Telegram отправил код подтверждения в официальное приложение.",
          reader: "Авторизация",
          session: "Нужен код",
        });
        showPanel("code");
        codeInput.focus();
        break;

      case "password_required":
        if (!flowId) {
          throw new ApiError("Сервер не вернул идентификатор входа. Начните авторизацию заново.");
        }
        setStatus({
          tone: "warning",
          title: "Нужен пароль 2FA",
          message: payload.message || "Введите пароль двухэтапной защиты Telegram.",
          reader: "Авторизация",
          session: "Нужен 2FA",
        });
        showPanel("password");
        passwordInput.focus();
        break;

      case "authorized":
        flowId = null;
        replaceExisting = false;
        phoneInput.value = "";
        codeInput.value = "";
        passwordInput.value = "";
        setStatus({
          tone: "success",
          title: "Сессия Reader сохранена",
          message: payload.message || "Аккаунт проверен. Зашифрованная сессия хранится на BotHost; сборщик запускается отдельно.",
          reader: "Не запущен",
          session: "Сохранена",
        });
        showPanel("success");
        break;

      case "locked":
        setStatus({
          tone: "warning",
          title: "Reader на паузе",
          message: payload.message || "Telegram временно ограничил новые попытки входа.",
          reader: "Пауза",
          session: "Заблокирована",
        });
        lockedMessage.textContent = payload.message || "Подождите указанное время перед новой проверкой.";
        showPanel("locked");
        startLockTimer(payload.retryAfter);
        break;
    }
  }

  function handleError(error) {
    if (error instanceof ApiError && error.status === 429) {
      applyState({
        state: "locked",
        flowId,
        message: error.message,
        retryAfter: error.retryAfter,
      });
      return;
    }

    if (error instanceof ApiError && error.status === 401) {
      flowId = null;
      setStatus({
        tone: "danger",
        title: "Доступ Telegram истёк",
        message: "Полностью закройте Mini App и откройте его снова из служебного бота.",
        reader: "Нет доступа",
        session: "Не проверена",
      });
      showPanel("telegramRequired");
      showError("Текущие данные Mini App больше не действуют. Переоткройте приложение в Telegram.");
      return;
    }

    const message = error instanceof ApiError
      ? error.message
      : "Произошла непредвиденная ошибка. Повторите попытку.";
    showError(message);
  }

  async function refreshStatus() {
    if (busy) return;
    setBusy(true);
    clearError();
    try {
      applyState(await apiRequest(API.status));
    } catch (error) {
      handleError(error);
    } finally {
      setBusy(false);
    }
  }

  phoneForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;

    const phone = phoneInput.value.replace(/[\s()\-]/g, "");
    if (!/^\+[1-9][0-9]{7,14}$/.test(phone)) {
      phoneInput.setAttribute("aria-invalid", "true");
      showError("Введите номер в международном формате, например +79991234567.");
      phoneInput.focus();
      return;
    }

    phoneInput.removeAttribute("aria-invalid");
    setBusy(true);
    clearError();
    try {
      const payload = await apiRequest(API.phone, {
        method: "POST",
        body: { phone, replace: replaceExisting },
      });
      phoneInput.value = "";
      applyState(payload);
    } catch (error) {
      handleError(error);
    } finally {
      setBusy(false);
    }
  });

  codeForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;

    const code = codeInput.value.replace(/\s/g, "");
    codeInput.value = "";
    if (!/^[0-9]{5,8}$/.test(code)) {
      showError("Введите цифровой код из Telegram.");
      codeInput.focus();
      return;
    }
    if (!flowId) {
      showError("Сессия входа устарела. Отмените вход и начните заново.");
      return;
    }

    setBusy(true);
    clearError();
    try {
      applyState(await apiRequest(API.code, {
        method: "POST",
        body: { flow_id: flowId, code },
      }));
    } catch (error) {
      handleError(error);
    } finally {
      setBusy(false);
    }
  });

  passwordForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;

    const password = passwordInput.value;
    passwordInput.value = "";
    passwordInput.type = "password";
    passwordToggle.textContent = "Показать";
    passwordToggle.setAttribute("aria-label", "Показать пароль");
    if (!password || password.length > 256) {
      showError("Введите пароль двухэтапной защиты.");
      passwordInput.focus();
      return;
    }
    if (!flowId) {
      showError("Сессия входа устарела. Отмените вход и начните заново.");
      return;
    }

    setBusy(true);
    clearError();
    try {
      applyState(await apiRequest(API.password, {
        method: "POST",
        body: { flow_id: flowId, password },
      }));
    } catch (error) {
      handleError(error);
    } finally {
      setBusy(false);
    }
  });

  document.querySelectorAll(".cancel-button").forEach((button) => {
    button.addEventListener("click", async () => {
      if (busy) return;
      if (!flowId) {
        await refreshStatus();
        return;
      }

      const currentFlowId = flowId;
      flowId = null;
      codeInput.value = "";
      passwordInput.value = "";
      setBusy(true);
      clearError();
      try {
        applyState(await apiRequest(API.cancel, {
          method: "POST",
          body: { flow_id: currentFlowId },
        }));
      } catch (error) {
        handleError(error);
      } finally {
        setBusy(false);
      }
    });
  });

  passwordToggle.addEventListener("click", () => {
    const willShow = passwordInput.type === "password";
    passwordInput.type = willShow ? "text" : "password";
    passwordToggle.textContent = willShow ? "Скрыть" : "Показать";
    passwordToggle.setAttribute("aria-label", willShow ? "Скрыть пароль" : "Показать пароль");
    passwordInput.focus();
  });

  refreshButton.addEventListener("click", refreshStatus);
  successRefreshButton.addEventListener("click", refreshStatus);
  reauthorizeButton.addEventListener("click", () => {
    if (busy) return;
    replaceExisting = true;
    flowId = null;
    clearError();
    setStatus({
      tone: "warning",
      title: "Переподключение Reader",
      message: "Старая зашифрованная сессия сохранится до полного успешного входа.",
      reader: "Ожидает вход",
      session: "Старая сохранена",
    });
    showPanel("phone");
    phoneInput.focus();
  });
  lockedRefreshButton.addEventListener("click", refreshStatus);

  if (webApp) {
    webApp.ready();
    webApp.expand();
  }

  if (!initData) {
    setBusy(false);
    setStatus({
      tone: "danger",
      title: "Нужен запуск из Telegram",
      message: "Откройте Mini App из служебного бота, чтобы сервер мог проверить администратора.",
      reader: "Недоступен",
      session: "Не проверена",
    });
    showPanel("telegramRequired");
  } else {
    refreshStatus();
  }
})();
