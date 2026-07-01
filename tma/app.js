"use strict";

let telegram = window.Telegram?.WebApp || null;

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";
const chatSummaryElement = document.querySelector("#chat-summary");
const appContentElement = document.querySelector("#app-content");

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
    "Открой Клевер через кнопку /app в нужном Telegram-чате. " +
    "Прямая ссылка в браузере не содержит контекст конкурса."
  );
}

function buildMissingChatContextMessage() {
  return (
    "Не удалось определить чат конкурса. " +
    "Открой приложение через свежую кнопку /app в нужном чате."
  );
}

function buildExpiredLaunchTokenMessage() {
  return (
    "Кнопка устарела. Отправь /app в нужном чате " +
    "и открой приложение через новую кнопку."
  );
}

function createElement(tagName, { className, text } = {}) {
  const element = document.createElement(tagName);

  if (className) {
    element.className = className;
  }

  if (text) {
    element.textContent = text;
  }

  return element;
}

function createStatusCard(title, message) {
  const card = createElement("section", {
    className: "status-card",
  });
  const indicator = createElement("div", {
    className: "status-indicator",
  });
  const content = createElement("div");
  const heading = createElement("h2", {
    text: title,
  });
  const description = createElement("p", {
    text: message,
  });

  indicator.setAttribute("aria-hidden", "true");
  content.append(heading, description);
  card.append(indicator, content);

  return card;
}

function createInfoCard(title, messages) {
  const card = createElement("section", {
    className: "info-card",
  });
  const heading = createElement("h2", {
    text: title,
  });

  card.append(heading);

  for (const message of messages) {
    card.append(
      createElement("p", {
        className: "subtitle",
        text: message,
      }),
    );
  }

  return card;
}

function renderLoading() {
  chatSummaryElement.textContent = "Загружаем конкурсы этого чата…";
  appContentElement.replaceChildren(
    createStatusCard(
      "Открываем конкурсы",
      "Проверяем доступ к конкурсам этого чата…",
    ),
  );
}

function getUserDisplayName(user) {
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ");

  return fullName || "участник";
}

function renderEmptyContests() {
  appContentElement.replaceChildren(
    createInfoCard("В этом чате пока нет конкурсов", [
      "Здесь будут отображаться все активные футбольные конкурсы этого чата.",
      "Создание первого конкурса появится на следующем шаге.",
    ]),
  );
}

function renderContestList(contests) {
  const card = createElement("section", {
    className: "info-card",
  });
  const heading = createElement("h2", {
    text: "Активные конкурсы",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Здесь отображаются все активные футбольные конкурсы этого чата. Открытие конкурса появится на следующем шаге.",
  });
  const list = createElement("ol");

  for (const contest of contests) {
    const item = createElement("li");
    const name = createElement("strong", {
      text: contest.name,
    });

    item.append(name);
    list.append(item);
  }

  card.append(heading, description, list);
  appContentElement.replaceChildren(card);
}

function renderBootstrap(bootstrap) {
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);
  const activeContests = Array.isArray(bootstrap.active_contests)
    ? bootstrap.active_contests
    : [];

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;

  if (activeContests.length === 0) {
    renderEmptyContests();
    return;
  }

  renderContestList(activeContests);
}

function renderError(message) {
  chatSummaryElement.textContent = "Не удалось открыть конкурсы.";

  appContentElement.replaceChildren(
    createInfoCard("Не удалось открыть Клевер", [message]),
  );
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
    const detail =
      body && typeof body === "object"
        ? body.detail
        : body;

    throw new Error(detail || `HTTP ${response.status}`);
  }

  return body;
}

function handleError(error) {
  const message =
    error instanceof Error
      ? error.message
      : "Не удалось открыть Клевер.";

  if (message === "Telegram init data start_param is required.") {
    renderError(buildMissingChatContextMessage());
    return;
  }

  if (message === "TMA launch token is expired.") {
    renderError(buildExpiredLaunchTokenMessage());
    return;
  }

  renderError(message);
}

async function initialize() {
  renderLoading();

  try {
    const bootstrap = await apiRequest("/api/tma/bootstrap");
    renderBootstrap(bootstrap);
  } catch (error) {
    handleError(error);
  }
}

void initialize();