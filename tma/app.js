"use strict";

let telegram = window.Telegram?.WebApp || null;

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";

const statusElement = document.querySelector("#app-status");

function refreshTelegramWebApp() {
  telegram = window.Telegram?.WebApp || telegram || null;
  return telegram;
}

function getTelegramLocationParams() {
  const hash = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;

  return {
    hashParams: new URLSearchParams(hash),
    searchParams: new URLSearchParams(window.location.search),
  };
}

function getTelegramInitData() {
  const sdkInitData = refreshTelegramWebApp()?.initData || "";

  if (sdkInitData) {
    return sdkInitData;
  }

  const { hashParams, searchParams } = getTelegramLocationParams();

  return (
    hashParams.get(TELEGRAM_INIT_DATA_QUERY_PARAM) ||
    searchParams.get(TELEGRAM_INIT_DATA_QUERY_PARAM) ||
    ""
  );
}

function buildMissingInitDataMessage() {
  return (
    "Открой Прогнозист через кнопку /app в нужном Telegram-чате. "
    + "Прямая ссылка в браузере не содержит контекст конкурса."
  );
}

function buildMissingChatContextMessage() {
  return (
    "Не удалось определить конкурс. "
    + "Открой приложение через свежую кнопку /app в нужном чате."
  );
}

function buildExpiredLaunchTokenMessage() {
  return (
    "Кнопка устарела. Отправь /app в нужном чате "
    + "и открой приложение через новую кнопку."
  );
}

function setStatus(message) {
  statusElement.textContent = message;
}

function initializeTelegramWebApp() {
  const currentTelegram = refreshTelegramWebApp();

  if (!currentTelegram) {
    return;
  }

  currentTelegram.ready();
  currentTelegram.expand();

  const updateViewportHeight = () => {
    document.documentElement.style.setProperty(
      "--tg-viewport-height",
      `${currentTelegram.viewportHeight}px`,
    );
  };

  updateViewportHeight();
  currentTelegram.onEvent?.("viewportChanged", updateViewportHeight);
}

async function apiRequest(path, options = {}) {
  const initData = getTelegramInitData();

  if (!initData) {
    throw new Error(buildMissingInitDataMessage());
  }

  const response = await fetch(path, {
    ...options,
    headers: {
      "X-Telegram-Init-Data": initData,
      ...(options.headers || {}),
    },
  });

  const contentType = response.headers.get("content-type") || "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail = typeof body === "object" ? body.detail : body;
    throw new Error(detail || `HTTP ${response.status}`);
  }

  return body;
}

function getUserDisplayName(user) {
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ");

  return fullName || "участник";
}

function renderBootstrap(bootstrap) {
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);

  setStatus(
    `Привет, ${userName}. Открыт конкурс чата «${chatTitle}». `
    + "Матчи и прогнозы появятся здесь после настройки конкурса.",
  );
}

function handleError(error) {
  if (error.message === "Telegram init data start_param is required.") {
    setStatus(buildMissingChatContextMessage());
    return;
  }

  if (error.message === "TMA launch token is expired.") {
    setStatus(buildExpiredLaunchTokenMessage());
    return;
  }

  setStatus(error.message || "Не удалось открыть Прогнозист.");
}

async function initialize() {
  initializeTelegramWebApp();
  setStatus("Проверяем доступ к конкурсу...");

  try {
    const bootstrap = await apiRequest("/api/tma/bootstrap");
    renderBootstrap(bootstrap);
  } catch (error) {
    handleError(error);
  }
}

void initialize();