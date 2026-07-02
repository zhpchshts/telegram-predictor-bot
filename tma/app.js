"use strict";

let telegram = window.Telegram?.WebApp || null;

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
const CONTEST_NAME_MAX_LENGTH = 80;

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
    "Открой Клевер через кнопку /app в нужном Telegram-чате.\n" +
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
    "Кнопка устарела.\n" +
    "Отправь /app в нужном чате и открой приложение через новую кнопку."
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

function createInfoCard(title, messages, className = "") {
  const card = createElement("section", {
    className: ["info-card", className].filter(Boolean).join(" "),
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

function createActionButton(text, className, type = "button") {
  const button = createElement("button", {
    className: `action-button ${className}`,
    text,
  });

  button.type = type;

  return button;
}

function setFormMessage(messageElement, message, type = "") {
  messageElement.textContent = message || "";
  messageElement.hidden = !message;
  messageElement.classList.toggle("is-error", type === "error");
  messageElement.classList.toggle("is-success", type === "success");
}

function normalizeContestName(value) {
  return value.replace(/\s+/g, " ").trim();
}

function createIdempotencyKey() {
  if (window.crypto?.randomUUID) {
    return `contest-${window.crypto.randomUUID()}`;
  }

  return `contest-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getUserDisplayName(user) {
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ");

  return fullName || "участник";
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

function createContestsCard(contests) {
  if (contests.length === 0) {
    return createInfoCard(
      "В этом чате пока нет конкурсов",
      [
        "Создай первый конкурс прогнозов на Чемпионат мира 2026.",
        "Позже здесь можно будет вести несколько параллельных конкурсов.",
      ],
      "contest-list-card",
    );
  }

  const card = createElement("section", {
    className: "info-card contest-list-card",
  });
  const heading = createElement("h2", {
    text: "Активные конкурсы",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: (
      "Здесь отображаются все активные футбольные конкурсы этого чата. " +
      "Открытие конкурса появится следующим шагом."
    ),
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

  return card;
}

function createContestFormCard(bootstrap, state) {
  const card = createElement("section", {
    className: "info-card contest-form-card",
  });
  const heading = createElement("h2", {
    text: "Создать конкурс",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Первый вариант — конкурс прогнозов на Чемпионат мира 2026.",
  });
  const form = createElement("form", {
    className: "form-fields",
  });
  const field = createElement("label", {
    className: "form-field",
  });
  const fieldLabel = createElement("span", {
    className: "form-field-label",
    text: "Название конкурса",
  });
  const input = createElement("input", {
    className: "text-input",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: (
      "Например: «ЧМ-2026: прогнозы». " +
      "Правила первого конкурса: 3 очка за точный счёт, " +
      "2 — за разницу голов, 1 — за исход."
    ),
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const continueButton = createActionButton(
    "Продолжить",
    "primary-action-button",
    "submit",
  );

  input.id = "contest-name";
  input.name = "contest-name";
  input.type = "text";
  input.maxLength = CONTEST_NAME_MAX_LENGTH;
  input.autocomplete = "off";
  input.placeholder = "ЧМ-2026: прогнозы";
  input.value = state.draftName || "";
  input.required = true;

  setFormMessage(
    message,
    state.formMessage || "",
    state.formMessageType || "",
  );

  field.append(fieldLabel, input);
  actions.append(continueButton);
  form.append(field, hint, message, actions);
  card.append(heading, description, form);

  form.addEventListener("submit", (event) => {
    event.preventDefault();

    const contestName = normalizeContestName(input.value);

    if (!contestName) {
      input.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Введите название конкурса.", "error");
      input.focus();
      return;
    }

    input.removeAttribute("aria-invalid");

    renderContestScreen(bootstrap, {
      mode: "confirm",
      draftName: contestName,
      idempotencyKey: createIdempotencyKey(),
    });
  });

  return card;
}

function createContestConfirmationCard(bootstrap, state) {
  const card = createElement("section", {
    className: "info-card contest-form-card",
  });
  const heading = createElement("h2", {
    text: "Подтвердить создание",
  });
  const panel = createElement("div", {
    className: "confirmation-panel",
  });
  const summary = createElement("p");
  const summaryName = createElement("strong", {
    text: state.draftName,
  });
  const details = createElement("p", {
    className: "form-hint",
    text: (
      "Будет создан активный конкурс на Чемпионат мира 2026 " +
      "со стандартными правилами начисления очков."
    ),
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const backButton = createActionButton(
    "Назад",
    "secondary-action-button",
  );
  const createButton = createActionButton(
    "Создать конкурс",
    "primary-action-button",
    "submit",
  );

  summary.append("Создать конкурс «", summaryName, "»?");

  setFormMessage(
    message,
    state.confirmationMessage || "",
    state.confirmationMessageType || "",
  );

  panel.append(summary, details, message);
  actions.append(backButton, createButton);
  card.append(heading, panel, actions);

  backButton.addEventListener("click", () => {
    renderContestScreen(bootstrap, {
      mode: "form",
      draftName: state.draftName,
    });
  });

  createButton.addEventListener("click", async () => {
    createButton.disabled = true;
    backButton.disabled = true;
    createButton.textContent = "Создаём…";

    try {
      const result = await apiRequest("/api/tma/contests", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [IDEMPOTENCY_KEY_HEADER]: state.idempotencyKey,
        },
        body: JSON.stringify({
          name: state.draftName,
        }),
      });

      if (!result || !result.contest) {
        throw new Error("Сервер вернул некорректный ответ при создании конкурса.");
      }

      const existingContests = Array.isArray(bootstrap.active_contests)
        ? bootstrap.active_contests
        : [];
      const nextBootstrap = {
        ...bootstrap,
        active_contests: [
          result.contest,
          ...existingContests.filter(
            (contest) => contest.id !== result.contest.id,
          ),
        ],
      };

      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: result.was_created
          ? `Конкурс «${result.contest.name}» создан.`
          : `Конкурс «${result.contest.name}» уже был создан ранее.`,
        formMessageType: "success",
      });
    } catch (error) {
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось создать конкурс.";

      renderContestScreen(bootstrap, {
        ...state,
        mode: "confirm",
        confirmationMessage: errorMessage,
        confirmationMessageType: "error",
      });
    }
  });

  return card;
}

function renderContestScreen(bootstrap, state = {}) {
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);
  const activeContests = Array.isArray(bootstrap.active_contests)
    ? bootstrap.active_contests
    : [];

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;

  const contestCard = createContestsCard(activeContests);
  const creationCard = state.mode === "confirm"
    ? createContestConfirmationCard(bootstrap, state)
    : createContestFormCard(bootstrap, state);

  appContentElement.replaceChildren(contestCard, creationCard);
}

function renderBootstrap(bootstrap) {
  renderContestScreen(bootstrap);
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
    const detail = body && typeof body === "object"
      ? body.detail
      : body;

    throw new Error(detail || `HTTP ${response.status}`);
  }

  return body;
}

function handleError(error) {
  const message = error instanceof Error
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