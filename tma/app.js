"use strict";

let telegram = window.Telegram?.WebApp || null;
let activeBootstrap = null;
let currentViewMode = "participant";

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
const CONTEST_NAME_MAX_LENGTH = 80;
const CONTEST_TABS = [
  { id: "predictions", label: "Прогнозы" },
  { id: "leaderboard", label: "Рейтинг" },
  { id: "matches", label: "Матчи" },
];
const AUDIT_EVENT_PRESENTATIONS = Object.freeze({
  contest_created: Object.freeze({
    label: "Создан конкурс",
    group: "Конкурсы",
  }),
  contest_updated: Object.freeze({
    label: "Изменены настройки конкурса",
    group: "Конкурсы",
  }),
  contest_finished: Object.freeze({
    label: "Конкурс завершён",
    group: "Конкурсы",
  }),
  contest_deleted: Object.freeze({
    label: "Удалён конкурс",
    group: "Конкурсы",
  }),
  match_created: Object.freeze({
    label: "Создан матч",
    group: "Матчи",
  }),
  match_updated: Object.freeze({
    label: "Изменено время матча",
    group: "Матчи",
  }),
  match_deleted: Object.freeze({
    label: "Удалён матч",
    group: "Матчи",
  }),
  match_result_set: Object.freeze({
    label: "Внесён результат матча",
    group: "Результаты",
  }),
  match_result_changed: Object.freeze({
    label: "Изменён результат матча",
    group: "Результаты",
  }),
  contest_champion_set: Object.freeze({
    label: "Указан чемпион",
    group: "Чемпион",
  }),
  contest_champion_changed: Object.freeze({
    label: "Изменён чемпион",
    group: "Чемпион",
  }),
  swiss_stage_settings_updated: Object.freeze({
    label: "Изменены настройки швейцарского этапа",
    group: "Швейцарский этап",
  }),
  swiss_stage_result_set: Object.freeze({
    label: "Внесены итоги швейцарского этапа",
    group: "Швейцарский этап",
  }),
  swiss_stage_result_changed: Object.freeze({
    label: "Исправлены итоги швейцарского этапа",
    group: "Швейцарский этап",
  }),
  supermoderator_assigned: Object.freeze({
    label: "Назначен супермодератор",
    group: "Доступ",
  }),
  supermoderator_revoked: Object.freeze({
    label: "Отозвана роль супермодератора",
    group: "Доступ",
  }),
});
const AUDIT_ROLE_LABELS = Object.freeze({
  telegram_admin: "Администратор Telegram",
  supermoderator: "Супермодератор",
  participant: "Участник",
});
const AUDIT_PAGE_SIZE = 30;

const chatSummaryElement = document.querySelector("#chat-summary");
const appContentElement = document.querySelector("#app-content");

function setChatSummary(text = "") {
  chatSummaryElement.textContent = text;
  chatSummaryElement.hidden = !text;
  if (chatSummaryElement.parentElement) {
    chatSummaryElement.parentElement.hidden = !text;
  }
}

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

function normalizeTeamName(value) {
  return value.replace(/\s+/g, " ").trim();
}

function formatMatchStartsAt(startsAtUtc) {
  const startsAt = new Date(startsAtUtc);

  if (Number.isNaN(startsAt.getTime())) {
    return startsAtUtc;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(startsAt);
}

function getRussianPlural(value, one, few, many) {
  const absoluteValue = Math.abs(value);
  const lastTwoDigits = absoluteValue % 100;
  const lastDigit = absoluteValue % 10;

  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) {
    return many;
  }
  if (lastDigit === 1) {
    return one;
  }
  if (lastDigit >= 2 && lastDigit <= 4) {
    return few;
  }
  return many;
}

function formatMatchStartsIn(match, now = new Date()) {
  if (match.status !== "scheduled") {
    return "";
  }

  const startsAt = new Date(match.starts_at_utc);
  const remainingMilliseconds = startsAt.getTime() - now.getTime();

  if (
    Number.isNaN(startsAt.getTime())
    || Number.isNaN(now.getTime())
    || remainingMilliseconds <= 0
  ) {
    return "";
  }

  const totalMinutes = Math.floor(remainingMilliseconds / 60_000);
  const days = Math.floor(totalMinutes / (24 * 60));
  const hours = Math.floor((totalMinutes % (24 * 60)) / 60);
  const minutes = totalMinutes % 60;

  return (
    "Начнётся через "
    + `${days} ${getRussianPlural(days, "день", "дня", "дней")}, `
    + `${hours} ${getRussianPlural(hours, "час", "часа", "часов")}, `
    + `${minutes} ${getRussianPlural(minutes, "минуту", "минуты", "минут")}`
  );
}

function formatRoleAssignmentDate(value) {
  const date = new Date(value.endsWith("Z") ? value : `${value}Z`);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function createIdempotencyKey(scope = "contest") {
  if (window.crypto?.randomUUID) {
    return `${scope}-${window.crypto.randomUUID()}`;
  }

  return `${scope}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getUserDisplayName(user) {
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ");

  return fullName || "участник";
}

function renderLoading() {
  setChatSummary("Загружаем конкурсы этого чата…");
  appContentElement.replaceChildren(
    createStatusCard(
      "Открываем конкурсы",
      "Проверяем доступ к конкурсам этого чата…",
    ),
  );
}

function createContestListCard(
  title,
  description,
  contests,
  onOpenContest,
  emptyMessage,
  className = "",
) {
  const card = createElement("section", {
    className: ["info-card", "contest-list-card", className]
      .filter(Boolean)
      .join(" "),
  });
  const heading = createElement("h2", {
    text: title,
  });
  const descriptionElement = createElement("p", {
    className: "subtitle",
    text: description,
  });

  card.append(heading, descriptionElement);

  if (contests.length === 0) {
    card.append(
      createElement("p", {
        className: "subtitle",
        text: emptyMessage,
      }),
    );
    return card;
  }

  const list = createElement("ol", {
    className: "contest-list",
  });

  for (const contest of contests) {
    const item = createElement("li", {
      className: "contest-list-item",
    });
    const button = createActionButton(
      contest.name,
      "secondary-action-button contest-list-button",
    );

    button.addEventListener("click", () => {
      void onOpenContest(contest.id);
    });

    item.append(button);
    list.append(item);
  }

  card.append(list);
  return card;
}

function createContestsCard(
  activeContests,
  completedContests,
  onOpenContest,
) {
  const normalizedActiveContests = Array.isArray(activeContests)
    ? activeContests
    : [];
  const normalizedCompletedContests = Array.isArray(completedContests)
    ? completedContests
    : [];

  if (
    normalizedActiveContests.length === 0 &&
    normalizedCompletedContests.length === 0
  ) {
    return createInfoCard(
      "В этом чате пока нет конкурсов",
      [
        "Когда будет создан конкурс, он появится здесь.",
        "Позже здесь можно будет вести несколько параллельных конкурсов.",
      ],
      "contest-list-card",
    );
  }

  const container = createElement("div", {
    className: "contest-lists",
  });

  container.append(
    createContestListCard(
      "Активные конкурсы",
      normalizedActiveContests.length > 0
        ? "Открой конкурс, чтобы делать прогнозы и смотреть рейтинг."
        : "Здесь будут конкурсы, в которых ещё можно участвовать.",
      normalizedActiveContests,
      onOpenContest,
      "Сейчас нет активных конкурсов.",
    ),
  );

  if (normalizedCompletedContests.length > 0) {
    container.append(
      createContestListCard(
        "Завершённые конкурсы",
        "Результаты, рейтинг и прогнозы сохранены и доступны для просмотра.",
        normalizedCompletedContests,
        onOpenContest,
        "Сейчас нет завершённых конкурсов.",
        "completed-contest-list-card",
      ),
    );
  }

  return container;
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

    renderContestCreationState(bootstrap, {
      ...state,
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
    renderContestCreationState(bootstrap, {
      ...state,
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

      if (state.managementMode === true) {
        activeBootstrap = nextBootstrap;
        void openContest(nextBootstrap, result.contest.id, {
          managementMode: true,
        });
        return;
      }
      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: result.was_created
          ? `Конкурс «${result.contest.name}» создан.`
          : `Конкурс «${result.contest.name}» уже был создан ранее.`,
        formMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось создать конкурс.";

      renderContestCreationState(bootstrap, {
        ...state,
        mode: "confirm",
        confirmationMessage: errorMessage,
        confirmationMessageType: "error",
      });
    }
  });

  return card;
}

function renderContestCreationState(bootstrap, state) {
  if (state.managementMode === true) {
    renderContestCreationScreen(bootstrap, state);
    return;
  }
  renderContestScreen(bootstrap, state);
}

function renderContestCreationScreen(bootstrap, state = {}) {
  currentViewMode = "management";
  setChatSummary();
  const creationState = {
    ...state,
    managementMode: true,
  };
  const formCard = state.mode === "confirm"
    ? createContestConfirmationCard(bootstrap, creationState)
    : createContestFormCard(bootstrap, creationState);

  appContentElement.replaceChildren(
    createAdministrativeHeader(bootstrap, {
      title: "Создание конкурса",
      backLabel: "← К управлению",
      onBack: () => {
        void openManagement(bootstrap);
      },
    }),
    formCard,
  );
}

function openContestCreation(bootstrap, managementData) {
  renderContestCreationScreen(bootstrap, {
    managementMode: true,
    managementData,
    mode: "form",
  });
}

function createContestDetailsCard(contest, onBack) {
  const card = createElement("section", {
    className: "contest-overview",
  });
  const backButton = createActionButton(
    "← Все конкурсы",
    "contest-back-link",
  );
  const heading = createElement("h2", {
    text: contest.name,
  });

  backButton.addEventListener("click", onBack);
  card.append(backButton, heading);

  if (contest.is_active === false) {
    card.append(
      createElement("p", {
        className: "contest-status contest-status--completed",
        text: "Завершён · доступен только просмотр",
      }),
    );
  }

  return card;
}

function createContestCompletionCard(bootstrap, contest, state) {
  const card = createElement("section", {
    className: "info-card contest-completion-card",
  });
  const heading = createElement("h2", {
    text: "Завершить конкурс",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const isConfirming = state.completionMode === "confirm";

  setFormMessage(
    message,
    state.completionMessage || "",
    state.completionMessageType || "",
  );

  if (getChampionPrediction(contest).is_open) {
    card.append(
      heading,
      createElement("p", {
        className: "subtitle",
        text: "Конкурс можно завершить после закрытия прогнозов на чемпиона.",
      }),
      message,
    );
    return card;
  }

  if (!isConfirming) {
    const description = createElement("p", {
      className: "subtitle",
      text: (
        "После завершения конкурс исчезнет из активных и будет доступен "
        + "в разделе «Завершённые» только для просмотра."
      ),
    });
    const continueButton = createActionButton(
      "Завершить конкурс",
      "danger-action-button",
    );

    continueButton.addEventListener("click", () => {
      renderContestDetailsRoute(bootstrap, contest, {
        ...state,
        activeTab: "matches",
        completionMode: "confirm",
        completionMessage: "",
        completionMessageType: "",
        deletionMode: "",
        deletionMessage: "",
        deletionMessageType: "",
      });
    });

    actions.append(continueButton);
    card.append(heading, description, message, actions);
    return card;
  }

  const panel = createElement("div", {
    className: "confirmation-panel contest-completion-confirmation",
  });
  const summary = createElement("p");
  const contestName = createElement("strong", {
    text: contest.name,
  });
  const details = createElement("p", {
    className: "form-hint",
    text: (
      "Матчи, результаты, прогнозы, рейтинг и начисленные баллы сохранятся. "
      + "Изменять конкурс после завершения будет нельзя."
    ),
  });
  const cancelButton = createActionButton(
    "Отмена",
    "secondary-action-button",
  );
  const completeButton = createActionButton(
    "Да, завершить конкурс",
    "danger-action-button",
  );

  summary.append("Завершить конкурс «", contestName, "»?");
  panel.append(summary, details);
  actions.append(cancelButton, completeButton);
  card.append(heading, panel, message, actions);

  cancelButton.addEventListener("click", () => {
    renderContestDetailsRoute(bootstrap, contest, {
      ...state,
      activeTab: "matches",
      completionMode: "",
      completionMessage: "",
      completionMessageType: "",
    });
  });

  completeButton.addEventListener("click", async () => {
    completeButton.disabled = true;
    cancelButton.disabled = true;
    completeButton.textContent = "Завершаем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/complete`,
        {
          method: "POST",
        },
      );

      if (!result || !result.contest) {
        throw new Error(
          "Сервер вернул некорректный ответ при завершении конкурса.",
        );
      }

      const activeContests = Array.isArray(bootstrap.active_contests)
        ? bootstrap.active_contests
        : [];
      const completedContests = Array.isArray(bootstrap.completed_contests)
        ? bootstrap.completed_contests
        : [];
      const nextBootstrap = {
        ...bootstrap,
        active_contests: activeContests.filter(
          (activeContest) => activeContest.id !== result.contest.id,
        ),
        completed_contests: [
          result.contest,
          ...completedContests.filter(
            (completedContest) =>
              completedContest.id !== result.contest.id,
          ),
        ],
      };

      if (state.managementMode === true) {
        activeBootstrap = nextBootstrap;
        void openManagement(nextBootstrap, {
          formMessage: `Конкурс «${result.contest.name}» завершён.`,
          formMessageType: "success",
        });
        return;
      }
      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: `Конкурс «${result.contest.name}» завершён.`,
        formMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось завершить конкурс.";

      renderContestDetailsRoute(bootstrap, contest, {
        ...state,
        activeTab: "matches",
        completionMode: "confirm",
        completionMessage: errorMessage,
        completionMessageType: "error",
      });
    }
  });

  return card;
}

function createContestDeletionCard(bootstrap, contest, state) {
  const card = createElement("section", {
    className: "info-card contest-completion-card",
  });
  const heading = createElement("h2", {
    text: "Удалить конкурс",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const isConfirming = state.deletionMode === "confirm";

  setFormMessage(
    message,
    state.deletionMessage || "",
    state.deletionMessageType || "",
  );

  if (!isConfirming) {
    const description = createElement("p", {
      className: "subtitle",
      text: (
        "Удаление необратимо: вместе с конкурсом будут удалены матчи, "
        + "результаты, прогнозы, рейтинг и начисленные баллы."
      ),
    });
    const continueButton = createActionButton(
      "Удалить конкурс",
      "danger-action-button",
    );

    continueButton.addEventListener("click", () => {
      renderContestDetailsRoute(bootstrap, contest, {
        ...state,
        activeTab: "matches",
        completionMode: "",
        completionMessage: "",
        completionMessageType: "",
        deletionMode: "confirm",
        deletionMessage: "",
        deletionMessageType: "",
      });
    });

    actions.append(continueButton);
    card.append(heading, description, message, actions);

    return card;
  }

  const panel = createElement("div", {
    className: "confirmation-panel contest-completion-confirmation",
  });
  const summary = createElement("p");
  const contestName = createElement("strong", {
    text: contest.name,
  });
  const details = createElement("p", {
    className: "form-hint",
    text: (
      "Восстановить конкурс или его данные после удаления будет нельзя."
    ),
  });
  const cancelButton = createActionButton(
    "Отмена",
    "secondary-action-button",
  );
  const deleteButton = createActionButton(
    "Да, удалить конкурс",
    "danger-action-button",
  );

  summary.append("Удалить конкурс «", contestName, "»?");
  panel.append(summary, details);
  actions.append(cancelButton, deleteButton);
  card.append(heading, panel, message, actions);

  cancelButton.addEventListener("click", () => {
    renderContestDetailsRoute(bootstrap, contest, {
      ...state,
      activeTab: "matches",
      deletionMode: "",
      deletionMessage: "",
      deletionMessageType: "",
    });
  });

  deleteButton.addEventListener("click", async () => {
    deleteButton.disabled = true;
    cancelButton.disabled = true;
    deleteButton.textContent = "Удаляем…";

    try {
      await apiRequest(`/api/tma/contests/${contest.id}`, {
        method: "DELETE",
      });

      const activeContests = Array.isArray(bootstrap.active_contests)
        ? bootstrap.active_contests
        : [];
      const nextBootstrap = {
        ...bootstrap,
        active_contests: activeContests.filter(
          (activeContest) => activeContest.id !== contest.id,
        ),
      };

      if (state.managementMode === true) {
        activeBootstrap = nextBootstrap;
        void openManagement(nextBootstrap, {
          formMessage: `Конкурс «${contest.name}» удалён.`,
          formMessageType: "success",
        });
        return;
      }
      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: `Конкурс «${contest.name}» удалён.`,
        formMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось удалить конкурс.";

      renderContestDetailsRoute(bootstrap, contest, {
        ...state,
        activeTab: "matches",
        deletionMode: "confirm",
        deletionMessage: errorMessage,
        deletionMessageType: "error",
      });
    }
  });

  return card;
}

function createLeaderboardCard(
  leaderboard,
  championPrediction,
  swissStagePrediction,
) {
  const entries = Array.isArray(leaderboard) ? leaderboard : [];
  const longTermPredictionSlotsCount =
    (championPrediction?.is_enabled === true ? 1 : 0) +
    (swissStagePrediction?.is_enabled === true ? 1 : 0);

  if (entries.length === 0) {
    return createInfoCard(
      "Рейтинг",
      [
        "Участники появятся здесь после сохранения первого прогноза.",
      ],
      "leaderboard-card",
    );
  }

  const card = createElement("section", {
    className: "info-card leaderboard-card",
  });
  const heading = createElement("h2", {
    text: "Рейтинг",
  });
  const list = createElement("ul", {
    className: "leaderboard-list",
  });
  const disclosures = [];

  for (const entry of entries) {
    const place = Number.isSafeInteger(entry?.place) ? entry.place : 0;
    const participantName =
      typeof entry?.participant_name === "string" &&
      entry.participant_name
        ? entry.participant_name
        : "Участник";
    const totalPoints = Number.isSafeInteger(entry?.total_points)
      ? entry.total_points
      : 0;
    const matchPredictionsCount = Number.isSafeInteger(
      entry?.match_predictions_count,
    )
      ? entry.match_predictions_count
      : 0;
    const championPredictionCount = Number.isSafeInteger(
      entry?.champion_prediction_count,
    )
      ? entry.champion_prediction_count
      : 0;
    const swissStagePredictionCount = Number.isSafeInteger(
      entry?.swiss_stage_prediction_count,
    )
      ? entry.swiss_stage_prediction_count
      : 0;
    const totalMatchesCount = Number.isSafeInteger(
      entry?.total_matches_count,
    )
      ? entry.total_matches_count
      : 0;
    const predictionHistory = Array.isArray(entry?.prediction_history)
      ? entry.prediction_history
      : [];
    const championPredictionHistory =
      entry?.champion_prediction_history &&
      typeof entry.champion_prediction_history === "object"
        ? entry.champion_prediction_history
        : null;
    const swissStagePredictionHistory =
      entry?.swiss_stage_prediction_history &&
      typeof entry.swiss_stage_prediction_history === "object"
        ? entry.swiss_stage_prediction_history
        : null;
    const hasPredictionHistory =
      predictionHistory.length > 0 ||
      championPredictionHistory !== null ||
      swissStagePredictionHistory !== null;

    const item = createElement("li", {
      className: "leaderboard-list-item",
    });
    const row = createLeaderboardRow(
      place,
      participantName,
      matchPredictionsCount,
      championPredictionCount + swissStagePredictionCount,
      totalMatchesCount,
      longTermPredictionSlotsCount,
      totalPoints,
    );

    if (!hasPredictionHistory) {
      row.classList.add("leaderboard-summary--static");
      item.append(row);
    } else {
      const disclosure = document.createElement("details");
      const summary = document.createElement("summary");
      const history = createLeaderboardPredictionHistory(
        predictionHistory,
        championPredictionHistory,
        swissStagePredictionHistory,
      );

      disclosure.className = "leaderboard-disclosure";
      summary.className = "leaderboard-summary";
      summary.append(row);
      disclosure.append(summary, history);
      item.append(disclosure);
      disclosures.push(disclosure);
    }

    list.append(item);
  }

  for (const disclosure of disclosures) {
    disclosure.addEventListener("toggle", () => {
      if (!disclosure.open) {
        return;
      }

      for (const otherDisclosure of disclosures) {
        if (otherDisclosure !== disclosure) {
          otherDisclosure.open = false;
        }
      }
    });
  }

  card.append(heading, list);
  return card;
}

function createLeaderboardRow(
  place,
  participantName,
  matchPredictionsCount,
  championPredictionCount,
  totalMatchesCount,
  longTermPredictionSlotsCount,
  totalPoints,
) {
  const row = createElement("div", {
    className: "leaderboard-row",
  });
  const placeElement = createElement("span", {
    className: "leaderboard-place",
    text: `${place}.`,
  });
  const participantElement = createElement("div", {
    className: "leaderboard-participant",
  });
  const nameElement = createElement("span", {
    className: "leaderboard-participant-name",
    text: participantName,
  });
  const predictionsElement = createElement("span", {
    className: "leaderboard-predictions",
    text: (
      `Прогнозов: ${matchPredictionsCount}+` +
      `${championPredictionCount} из ` +
      `${totalMatchesCount + longTermPredictionSlotsCount}`
    ),
  });
  const pointsElement = createElement("span", {
    className: "leaderboard-points",
    text: `${totalPoints} ${getPointsLabel(totalPoints)}`,
  });

  participantElement.append(nameElement, predictionsElement);
  row.append(placeElement, participantElement, pointsElement);
  return row;
}

function createLeaderboardPredictionHistory(
  predictionHistory,
  championPredictionHistory,
  swissStagePredictionHistory,
) {
  const list = createElement("ol", {
    className: "leaderboard-history",
  });

  for (const match of predictionHistory) {
    const prediction = match?.prediction;
    if (
      !Number.isSafeInteger(prediction?.home_score) ||
      !Number.isSafeInteger(prediction?.away_score)
    ) {
      continue;
    }

    const homeTeamName =
      typeof match?.home_team_name === "string" && match.home_team_name
        ? match.home_team_name
        : "Первая команда";
    const awayTeamName =
      typeof match?.away_team_name === "string" && match.away_team_name
        ? match.away_team_name
        : "Вторая команда";
    const result = match?.result;
    const item = createElement("li", {
      className: "leaderboard-history-match",
    });
    const header = createElement("div", {
      className: "leaderboard-history-header",
    });
    const title = createElement("span", {
      className: "leaderboard-history-title",
      text: `${homeTeamName} — ${awayTeamName}`,
    });
    const points = createElement("span", {
      className: "leaderboard-history-points",
      text: getLeaderboardMatchPoints(match),
    });
    const predictionRow = createLeaderboardHistoryRow(
      "Прогноз",
      formatLeaderboardMatchScore(match, prediction),
    );
    const resultRow = createLeaderboardHistoryRow(
      "Факт",
      result === null || result === undefined
        ? "Ожидает результата"
        : formatLeaderboardMatchScore(match, result),
    );

    header.append(title, points);
    item.append(header, predictionRow, resultRow);
    list.append(item);
  }

  if (championPredictionHistory !== null) {
    list.append(
      createLeaderboardChampionPredictionHistory(championPredictionHistory),
    );
  }
  if (swissStagePredictionHistory !== null) {
    list.append(
      createLeaderboardSwissStagePredictionHistory(
        swissStagePredictionHistory,
      ),
    );
  }

  return list;
}

function createLeaderboardHistoryRow(label, value) {
  const row = createElement("div", {
    className: "leaderboard-history-row",
  });
  const labelElement = createElement("span", {
    className: "leaderboard-history-label",
    text: label,
  });
  const valueElement = createElement("span", {
    className: "leaderboard-history-value",
    text: value,
  });

  row.append(labelElement, valueElement);
  return row;
}

function formatLeaderboardMatchScore(match, score) {
  if (
    !Number.isSafeInteger(score?.home_score) ||
    !Number.isSafeInteger(score?.away_score)
  ) {
    return "—";
  }

  const scoreText = `${score.home_score}:${score.away_score}`;
  if (score.home_score !== score.away_score) {
    return scoreText;
  }

  const advancingTeamName = getLeaderboardTeamName(
    match,
    score?.advancing_team_id,
  );
  return advancingTeamName
    ? `${scoreText} · проходит ${advancingTeamName}`
    : scoreText;
}

function getLeaderboardTeamName(match, teamId) {
  if (!Number.isSafeInteger(teamId)) {
    return null;
  }

  if (teamId === match?.home_team_id) {
    return match.home_team_name;
  }

  if (teamId === match?.away_team_id) {
    return match.away_team_name;
  }

  return null;
}

function createLeaderboardChampionPredictionHistory(championPrediction) {
  const predictionTeamName = getLeaderboardTeamSummaryName(
    championPrediction?.prediction,
  );
  const actualChampionName = getLeaderboardTeamSummaryName(
    championPrediction?.actual_champion,
  );
  const item = createElement("li", {
    className: "leaderboard-history-match",
  });
  const header = createElement("div", {
    className: "leaderboard-history-header",
  });
  const title = createElement("span", {
    className: "leaderboard-history-title",
    text: "Чемпион турнира",
  });
  const points = createElement("span", {
    className: "leaderboard-history-points",
    text: getLeaderboardChampionPredictionPoints(championPrediction),
  });
  const predictionRow = createLeaderboardHistoryRow(
    "Прогноз",
    predictionTeamName ?? "—",
  );
  const resultRow = createLeaderboardHistoryRow(
    "Факт",
    actualChampionName ?? "Ожидает результата",
  );

  header.append(title, points);
  item.append(header, predictionRow, resultRow);
  return item;
}

function getLeaderboardTeamSummaryName(team) {
  return typeof team?.name === "string" && team.name ? team.name : null;
}

function getLeaderboardChampionPredictionPoints(championPrediction) {
  const awardedPoints = championPrediction?.awarded_points;
  if (!Number.isSafeInteger(awardedPoints)) {
    return "—";
  }

  return awardedPoints > 0 ? `+${awardedPoints}` : "0";
}

function createLeaderboardSwissStagePredictionHistory(prediction) {
  const item = createElement("li", {
    className: "leaderboard-history-match",
  });
  const header = createElement("div", {
    className: "leaderboard-history-header",
  });
  header.append(
    createElement("span", {
      className: "leaderboard-history-title",
      text: "Итоги швейцарского этапа",
    }),
    createElement("span", {
      className: "leaderboard-history-points",
      text: Number.isSafeInteger(prediction?.awarded_points)
        ? `+${prediction.awarded_points}`
        : "—",
    }),
  );
  item.append(
    header,
    createLeaderboardHistoryRow(
      "Напрямую",
      prediction?.prediction?.direct_teams
        ?.map((team) => team.name)
        .join(", ") || "—",
    ),
    createLeaderboardHistoryRow(
      "Элиминейшн",
      prediction?.prediction?.elimination_teams
        ?.map((team) => team.name)
        .join(", ") || "—",
    ),
  );
  if (!prediction?.actual_result) {
    item.append(
      createLeaderboardHistoryRow("Баллы", "Ожидает результата"),
    );
  } else if (Array.isArray(prediction.awards) && prediction.awards.length > 0) {
    item.append(createSwissStageAwardsBreakdown(prediction.awards));
  }
  return item;
}

function getLeaderboardMatchPoints(match) {
  const totalPoints = match?.prediction_score?.total_points;
  if (!Number.isSafeInteger(totalPoints)) {
    return "—";
  }

  return totalPoints > 0 ? `+${totalPoints}` : "0";
}

function createContestRulesCard(championPrediction, swissStagePrediction) {
  const card = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryContent = createElement("div", {
    className: "contest-rules-summary-content",
  });
  const title = createElement("span", {
    className: "contest-rules-title",
    text: "Правила начисления",
  });
  const isChampionPredictionEnabled =
    championPrediction?.is_enabled === true;
  const championPoints = Number.isSafeInteger(championPrediction?.points)
    ? championPrediction.points
    : 5;
  const overview = createElement("span", {
    className: "contest-rules-overview",
    text: isChampionPredictionEnabled
      ? (
        "3 — счёт · 2 — разница · 1 — исход · +1 — победитель · " +
        `+${championPoints} — чемпион`
      )
      : "3 — счёт · 2 — разница · 1 — исход · +1 — победитель",
  });
  const body = createElement("div", {
    className: "contest-rules-body",
  });

  card.className = "contest-rules-card";

  summaryContent.append(title, overview);
  summary.append(summaryContent);
  body.append(
    createElement("p", {
      text: "Точный счёт — 3 балла; точная разница голов — 2; верный исход — 1.",
    }),
    createElement("p", {
      text: "За верно выбранного победителя противостояния — ещё 1 балл.",
    }),
  );

  if (isChampionPredictionEnabled) {
    body.append(
      createElement("p", {
        text: (
          `За верно выбранного чемпиона — ещё ${championPoints} ` +
          `${getPointsLabel(championPoints)}.`
        ),
      }),
    );
  }
  if (swissStagePrediction?.is_enabled === true) {
    body.append(
      createElement("p", {
        text: (
          "Швейцарский этап: 2 балла за команду и точный способ прохода, " +
          "1 балл — если способ прохода перепутан."
        ),
      }),
    );
  }

  body.append(
    createElement("p", {
      text: "Счёт учитывается после 90 или 120 минут. Голы серии пенальти в него не входят.",
    }),
    createElement("p", {
      text: "Прогноз на матч можно изменить до начала матча.",
    }),
  );

  card.append(summary, body);
  return card;
}

function getActiveContestTab(tab) {
  return CONTEST_TABS.some((candidate) => candidate.id === tab)
    ? tab
    : "predictions";
}

function createContestTabs(activeTab, onSelectTab) {
  const tabs = createElement("nav", {
    className: "contest-tabs",
  });
  const track = createElement("div", {
    className: "contest-tabs-track",
  });

  tabs.setAttribute("aria-label", "Разделы конкурса");

  for (const tab of CONTEST_TABS) {
    const button = createActionButton(tab.label, "contest-tab");

    if (tab.id === activeTab) {
      button.classList.add("is-active");
      button.setAttribute("aria-current", "page");
    }

    button.addEventListener("click", () => {
      onSelectTab(tab.id);
    });

    track.append(button);
  }

  tabs.append(track);
  return tabs;
}

function isMatchPredictionOpen(match) {
  if (match.status !== "scheduled") {
    return false;
  }

  const startsAt = new Date(match.starts_at_utc);

  return (
    !Number.isNaN(startsAt.getTime()) &&
    startsAt.getTime() > Date.now()
  );
}

function getMatchStatusLabel(status) {
  const labels = {
    scheduled: "Запланирован",
    started: "Идёт",
    finished: "Завершён",
    cancelled: "Отменён",
  };

  return labels[status] || status;
}

function isMatchResultAvailable(match) {
  if (match.status === "finished") {
    return true;
  }

  if (match.status !== "scheduled" && match.status !== "started") {
    return false;
  }

  const startsAt = new Date(match.starts_at_utc);
  return (
    !Number.isNaN(startsAt.getTime()) && startsAt.getTime() <= Date.now()
  );
}

function getTeamNameById(match, teamId) {
  if (teamId === match.home_team_id) {
    return match.home_team_name;
  }

  if (teamId === match.away_team_id) {
    return match.away_team_name;
  }

  return "Не определена";
}

function getNonNegativeIntegerInputValue(input) {
  const value = input.value.trim();

  if (!value) {
    return null;
  }

  const numberValue = Number(value);

  if (!Number.isSafeInteger(numberValue) || numberValue < 0) {
    return null;
  }

  return numberValue;
}

function getMatchScoreState(homeScoreInput, awayScoreInput) {
  const homeScore = getNonNegativeIntegerInputValue(homeScoreInput);
  const awayScore = getNonNegativeIntegerInputValue(awayScoreInput);

  if (homeScore === null || awayScore === null) {
    return {
      isComplete: false,
      isDraw: false,
      homeScore: null,
      awayScore: null,
    };
  }

  return {
    isComplete: true,
    isDraw: homeScore === awayScore,
    homeScore,
    awayScore,
  };
}

function createAdvancingTeamField(
  match,
  {
    idPrefix,
    homeScoreInput,
    awayScoreInput,
    selectedAdvancingTeamId = null,
    missingDrawSelectionMessage =
      "При ничьей выберите команду, победившую в серии пенальти.",
    selectedDrawSelectionMessage = null,
    highlightMissingDrawSelection = false,
  },
) {
  const field = createElement("fieldset", {
    className: "advancing-team-field",
  });
  const legend = createElement("legend", {
    className: "form-field-label",
    text: "Победитель противостояния",
  });
  const hint = createElement("p", {
    className: "form-hint",
  });
  const options = createElement("div", {
    className: "advancing-team-options",
  });
  const homeOption = createElement("label", {
    className: "advancing-team-option",
  });
  const homeRadio = createElement("input");
  const homeText = createElement("span", {
    text: match.home_team_name,
  });
  const awayOption = createElement("label", {
    className: "advancing-team-option",
  });
  const awayRadio = createElement("input");
  const awayText = createElement("span", {
    text: match.away_team_name,
  });
  const normalizedSelectedAdvancingTeamId =
    Number.isSafeInteger(selectedAdvancingTeamId)
      ? selectedAdvancingTeamId
      : null;

  homeRadio.id = `${idPrefix}-advancing-home`;
  homeRadio.name = `${idPrefix}-advancing-team`;
  homeRadio.type = "radio";
  homeRadio.value = String(match.home_team_id);
  homeRadio.checked =
    normalizedSelectedAdvancingTeamId === match.home_team_id;

  awayRadio.id = `${idPrefix}-advancing-away`;
  awayRadio.name = `${idPrefix}-advancing-team`;
  awayRadio.type = "radio";
  awayRadio.value = String(match.away_team_id);
  awayRadio.checked =
    normalizedSelectedAdvancingTeamId === match.away_team_id;

  hint.id = `${idPrefix}-advancing-hint`;
  hint.setAttribute("aria-live", "polite");
  homeRadio.setAttribute("aria-describedby", hint.id);
  awayRadio.setAttribute("aria-describedby", hint.id);

  homeOption.append(homeRadio, homeText);
  awayOption.append(awayRadio, awayText);
  options.append(homeOption, awayOption);
  field.append(legend, hint, options);

  let previousScoreState = getMatchScoreState(
    homeScoreInput,
    awayScoreInput,
  );

  function syncAdvancingTeamField(resetDrawSelection) {
    const scoreState = getMatchScoreState(
      homeScoreInput,
      awayScoreInput,
    );

    if (!scoreState.isComplete) {
      field.disabled = true;
      field.classList.remove("is-required");
      field.removeAttribute("aria-invalid");
      hint.textContent = "Сначала укажите итоговый счёт матча.";
    } else if (scoreState.isDraw) {
      field.disabled = false;

      if (resetDrawSelection && !previousScoreState.isDraw) {
        homeRadio.checked = false;
        awayRadio.checked = false;
      }

      const isDrawSelectionMissing = !homeRadio.checked && !awayRadio.checked;

      field.classList.toggle(
        "is-required",
        highlightMissingDrawSelection && isDrawSelectionMissing,
      );
      field.toggleAttribute(
        "aria-invalid",
        highlightMissingDrawSelection && isDrawSelectionMissing,
      );
      hint.textContent = isDrawSelectionMissing
        ? missingDrawSelectionMessage
        : selectedDrawSelectionMessage || missingDrawSelectionMessage;
    } else {
      const advancingTeamId =
        scoreState.homeScore > scoreState.awayScore
          ? match.home_team_id
          : match.away_team_id;

      homeRadio.checked = advancingTeamId === match.home_team_id;
      awayRadio.checked = advancingTeamId === match.away_team_id;
      field.disabled = true;
      field.classList.remove("is-required");
      field.removeAttribute("aria-invalid");
      hint.textContent =
        "Победитель противостояния определён итоговым счётом.";
    }

    previousScoreState = scoreState;
  }

  syncAdvancingTeamField(false);

  homeScoreInput.addEventListener("input", () => {
    syncAdvancingTeamField(true);
  });

  awayScoreInput.addEventListener("input", () => {
    syncAdvancingTeamField(true);
  });

  field.addEventListener("change", () => {
    syncAdvancingTeamField(false);
  });

  return {
    element: field,
    focus() {
      homeRadio.focus();
    },
    getAdvancingTeamId() {
      const selectedRadio = field.querySelector(
        'input[name="' + homeRadio.name + '"]:checked',
      );

      return selectedRadio ? Number(selectedRadio.value) : null;
    },
  };
}

function getPredictionScoreTypeLabel(scoreType) {
  const labels = {
    exact_score: "Точный счёт",
    goal_difference: "Разница голов",
    outcome: "Исход матча",
    advancing_team: "Победитель противостояния",
  };

  return labels[scoreType] || scoreType;
}

function getPointsLabel(points) {
  const absolutePoints = Math.abs(points);
  const lastTwoDigits = absolutePoints % 100;
  const lastDigit = absolutePoints % 10;

  if (lastTwoDigits >= 11 && lastTwoDigits <= 14) {
    return "баллов";
  }

  if (lastDigit === 1) {
    return "балл";
  }

  if (lastDigit >= 2 && lastDigit <= 4) {
    return "балла";
  }

  return "баллов";
}

function createPredictionScoreSummary(predictionScore) {
  const totalPoints = Number.isSafeInteger(predictionScore?.total_points)
    ? predictionScore.total_points
    : 0;
  const awards = Array.isArray(predictionScore?.awards)
    ? predictionScore.awards
    : [];
  const awardText = awards
    .map(
      (award) =>
        `${getPredictionScoreTypeLabel(award.type)} — +${award.points}`,
    )
    .join("; ");
  const summary = createElement("p", {
    className: "match-prediction-score",
    text: (
      `Начислено: ${totalPoints} ${getPointsLabel(totalPoints)}.` +
      (awardText ? ` ${awardText}.` : "")
    ),
  });

  return summary;
}

function createPredictionProgressSummary() {
  return createElement("p", {
    className: "form-hint match-prediction-progress",
    text: "Прогнозы сохранены: 0 из 0.",
  });
}

function updatePredictionProgress(summary) {
  if (!summary) {
    return;
  }

  const card = summary.closest(".matches-card");
  const statuses = Array.from(
    card?.querySelectorAll(".match-prediction-save-status") || [],
  );
  const total = statuses.length;
  const saved = statuses.filter(
    (status) => status.dataset.saveState === "saved",
  ).length;

  if (total === 0) {
    summary.textContent = "Нет матчей, для которых сейчас можно сделать прогноз.";
    return;
  }

  summary.textContent =
    saved === total
      ? `Прогнозы сохранены: ${saved} из ${total}.`
      : `Прогнозы сохранены: ${saved} из ${total}. Осталось: ${total - saved}.`;
}

function ensurePredictionProgressSummary(section) {
  const card = section.closest(".matches-card");
  if (!card) {
    return;
  }

  let summary = card.querySelector(".match-prediction-progress");
  if (!summary) {
    summary = createPredictionProgressSummary();
    const heading = card.querySelector(":scope > h2");
    if (heading) {
      heading.insertAdjacentElement("afterend", summary);
    } else {
      card.prepend(summary);
    }
  }

  updatePredictionProgress(summary);
}

function createMatchResultSection(
  contest,
  match,
  state,
  onResultSaved,
  canManageResults,
) {
  const result = match.result;
  const section = createElement("div", {
    className: "match-result-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Результат матча",
  });

  if (contest.is_active === false || !canManageResults) {
    const readOnlyMessage = createElement("p", {
      className: "match-prediction-closed",
      text: result
        ? (
          `Итоговый счёт: ${result.home_score} : ${result.away_score}. `
          + `Победитель противостояния: `
          + `${getTeamNameById(match, result.advancing_team_id)}.`
        )
        : contest.is_active === false
          ? "Конкурс завершён. Результаты доступны только для просмотра."
          : "Результат пока не внесён.",
    });

    section.append(heading, readOnlyMessage);
    return section;
  }

  if (match.status === "cancelled") {
    const unavailableMessage = createElement("p", {
      className: "match-prediction-closed",
      text: "Матч отменён. Результат недоступен.",
    });

    section.append(heading, unavailableMessage);
    return section;
  }

  if (!isMatchResultAvailable(match)) {
    const unavailableMessage = createElement("p", {
      className: "match-prediction-closed",
      text: "Результат можно внести после начала матча.",
    });

    section.append(heading, unavailableMessage);
    return section;
  }

  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryTitle = createElement("span", {
    className: "match-result-summary-title",
    text: result
      ? `Результат: ${result.home_score} : ${result.away_score}`
      : "Внести результат",
  });
  const summaryAction = createElement("span", {
    className: "match-result-summary-action",
    text: result ? "Изменить" : "Открыть",
  });
  const hint = createElement("p", {
    className: "match-prediction-hint",
    text: (
      "Укажите итоговый счёт после 90 или 120 минут. " +
      "Голы серии пенальти в него не входят. Результат можно исправить."
    ),
  });
  const form = createElement("form", {
    className: "match-prediction-form",
  });
  const scoreHeading = createElement("p", {
    className: "form-field-label",
    text: "Итоговый счёт матча",
  });
  const scoreGrid = createElement("div", {
    className: "match-score-grid",
  });
  const homeScoreField = createElement("label", {
    className: "match-score-field",
  });
  const homeScoreLabel = createElement("span", {
    className: "match-score-label",
    text: match.home_team_name,
  });
  const homeScoreInput = createElement("input", {
    className: "text-input match-score-input",
  });
  const awayScoreField = createElement("label", {
    className: "match-score-field",
  });
  const awayScoreLabel = createElement("span", {
    className: "match-score-label",
    text: match.away_team_name,
  });
  const awayScoreInput = createElement("input", {
    className: "text-input match-score-input",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  let submitLabel = result ? "Сохранить изменения" : "Сохранить результат";
  const submitButton = createActionButton(
    submitLabel,
    "primary-action-button",
    "submit",
  );

  disclosure.className = "match-result-disclosure";
  disclosure.open = state.resultMatchId === match.id;

  homeScoreInput.id = `match-${match.id}-result-home-score`;
  homeScoreInput.name = `match-${match.id}-result-home-score`;
  homeScoreInput.type = "number";
  homeScoreInput.min = "0";
  homeScoreInput.step = "1";
  homeScoreInput.inputMode = "numeric";
  homeScoreInput.value = result ? String(result.home_score) : "";
  homeScoreInput.required = true;

  awayScoreInput.id = `match-${match.id}-result-away-score`;
  awayScoreInput.name = `match-${match.id}-result-away-score`;
  awayScoreInput.type = "number";
  awayScoreInput.min = "0";
  awayScoreInput.step = "1";
  awayScoreInput.inputMode = "numeric";
  awayScoreInput.value = result ? String(result.away_score) : "";
  awayScoreInput.required = true;

  const advancingTeamField = createAdvancingTeamField(match, {
    idPrefix: `match-${match.id}-result`,
    homeScoreInput,
    awayScoreInput,
    selectedAdvancingTeamId: result
      ? result.advancing_team_id
      : null,
  });

  setFormMessage(
    message,
    state.resultMatchId === match.id ? state.resultMessage || "" : "",
    state.resultMatchId === match.id ? state.resultMessageType || "" : "",
  );

  homeScoreField.append(homeScoreLabel, homeScoreInput);
  awayScoreField.append(awayScoreLabel, awayScoreInput);
  scoreGrid.append(homeScoreField, awayScoreField);
  actions.append(submitButton);
  form.append(
    scoreHeading,
    scoreGrid,
    advancingTeamField.element,
    message,
    actions,
  );
  summary.append(summaryTitle, summaryAction);
  disclosure.append(summary, hint, form);
  section.append(heading, disclosure);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const homeScoreValue = homeScoreInput.value.trim();
    const awayScoreValue = awayScoreInput.value.trim();
    const homeScore = Number(homeScoreValue);
    const awayScore = Number(awayScoreValue);

    if (
      !homeScoreValue ||
      !Number.isSafeInteger(homeScore) ||
      homeScore < 0
    ) {
      homeScoreInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Укажите неотрицательный целый счёт первой команды.",
        "error",
      );
      homeScoreInput.focus();
      return;
    }

    homeScoreInput.removeAttribute("aria-invalid");

    if (
      !awayScoreValue ||
      !Number.isSafeInteger(awayScore) ||
      awayScore < 0
    ) {
      awayScoreInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Укажите неотрицательный целый счёт второй команды.",
        "error",
      );
      awayScoreInput.focus();
      return;
    }

    awayScoreInput.removeAttribute("aria-invalid");

    const advancingTeamId = advancingTeamField.getAdvancingTeamId();

    if (advancingTeamId === null) {
      setFormMessage(
        message,
        "При ничейном счёте выберите победителя противостояния.",
        "error",
      );
      advancingTeamField.focus();
      return;
    }

    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const resultPayload = await apiRequest(
        `/api/tma/contests/${contest.id}/matches/${match.id}/result`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            home_score: homeScore,
            away_score: awayScore,
            advancing_team_id: advancingTeamId,
          }),
        },
      );

      if (!resultPayload || !resultPayload.result) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении результата.",
        );
      }

      match.result = resultPayload.result;
      match.status = "finished";

      onResultSaved({
        matchId: match.id,
        message: resultPayload.was_created
          ? "Результат сохранён."
          : "Результат обновлён.",
        type: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить результат.";

      setFormMessage(message, errorMessage, "error");
      submitButton.textContent = submitLabel;
      submitButton.disabled = false;
    }
  });

  return section;
}

function createMatchPredictionSection(contest, match) {
  const prediction = match.prediction;
  const section = createElement("div", {
    className: "match-prediction-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Ваш прогноз",
  });

  if (contest.is_active === false || !isMatchPredictionOpen(match)) {
    const closedMessage = createElement("p", {
      className: "match-prediction-closed",
      text: prediction
        ? (
          `Ваш прогноз: ${prediction.home_score} : ` +
          `${prediction.away_score}. Победитель противостояния: ` +
          `${getTeamNameById(match, prediction.advancing_team_id)}.`
        )
        : contest.is_active === false
          ? "Конкурс завершён. Прогноз не был сохранён."
          : "Прогнозы на этот матч закрыты.",
    });

    if (prediction && match.prediction_score) {
      section.append(
        heading,
        closedMessage,
        createPredictionScoreSummary(match.prediction_score),
      );
    } else {
      section.append(heading, closedMessage);
    }

    return section;
  }

  const hint = createElement("p", {
    className: "match-prediction-hint",
    text: (
      "Прогноз сохраняется автоматически. Счёт указывайте после 90 или " +
      "120 минут; при ничьей выберите победителя серии пенальти. " +
      "Прогноз можно изменить до начала матча."
    ),
  });
  const form = createElement("form", {
    className: "match-prediction-form",
  });
  const scoreHeading = createElement("p", {
    className: "form-field-label",
    text: "Итоговый счёт матча",
  });
  const scoreGrid = createElement("div", {
    className: "match-score-grid",
  });
  const homeScoreField = createElement("label", {
    className: "match-score-field",
  });
  const homeScoreLabel = createElement("span", {
    className: "match-score-label",
    text: match.home_team_name,
  });
  const homeScoreInput = createElement("input", {
    className: "text-input match-score-input",
  });
  const awayScoreField = createElement("label", {
    className: "match-score-field",
  });
  const awayScoreLabel = createElement("span", {
    className: "match-score-label",
    text: match.away_team_name,
  });
  const awayScoreInput = createElement("input", {
    className: "text-input match-score-input",
  });
  const saveStatus = createElement("p", {
    className: "form-message match-prediction-save-status",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const retryButton = createActionButton(
    "Повторить сохранение",
    "secondary-action-button",
  );

  homeScoreInput.id = `match-${match.id}-home-score`;
  homeScoreInput.name = `match-${match.id}-home-score`;
  homeScoreInput.type = "number";
  homeScoreInput.min = "0";
  homeScoreInput.step = "1";
  homeScoreInput.inputMode = "numeric";
  homeScoreInput.value = prediction ? String(prediction.home_score) : "";
  homeScoreInput.required = true;

  awayScoreInput.id = `match-${match.id}-away-score`;
  awayScoreInput.name = `match-${match.id}-away-score`;
  awayScoreInput.type = "number";
  awayScoreInput.min = "0";
  awayScoreInput.step = "1";
  awayScoreInput.inputMode = "numeric";
  awayScoreInput.value = prediction ? String(prediction.away_score) : "";
  awayScoreInput.required = true;

  const advancingTeamField = createAdvancingTeamField(match, {
    idPrefix: `match-${match.id}-prediction`,
    homeScoreInput,
    awayScoreInput,
    selectedAdvancingTeamId: prediction ? prediction.advancing_team_id : null,
    missingDrawSelectionMessage:
      "Выберите победителя противостояния, чтобы сохранить прогноз.",
    selectedDrawSelectionMessage: "Победитель противостояния выбран.",
    highlightMissingDrawSelection: true,
  });

  function setSaveStatus(message, state) {
    saveStatus.textContent = message;
    saveStatus.hidden = !message;
    saveStatus.dataset.saveState = state;
    saveStatus.classList.toggle("is-error", state === "error");
    saveStatus.classList.toggle("is-success", state === "saved");
    retryButton.hidden = state !== "error";
    updatePredictionProgress(
      section.closest(".matches-card")?.querySelector(
        ".match-prediction-progress",
      ),
    );
  }

  function readPredictionPayload() {
    const homeScore = getNonNegativeIntegerInputValue(homeScoreInput);
    const awayScore = getNonNegativeIntegerInputValue(awayScoreInput);

    if (homeScore === null || awayScore === null) {
      return {
        isReady: false,
        message: "Укажите неотрицательный целый счёт обеих команд.",
      };
    }

    const advancingTeamId = advancingTeamField.getAdvancingTeamId();
    if (advancingTeamId === null) {
      return {
        isReady: false,
        message:
          "Выберите победителя противостояния, чтобы сохранить прогноз.",
      };
    }

    return {
      isReady: true,
      predicted_home_score: homeScore,
      predicted_away_score: awayScore,
      predicted_advancing_team_id: advancingTeamId,
    };
  }

  function getPayloadFingerprint(payload) {
    return [
      payload.predicted_home_score,
      payload.predicted_away_score,
      payload.predicted_advancing_team_id,
    ].join(":");
  }

  let isSaving = false;
  let lastSavedFingerprint = prediction
    ? getPayloadFingerprint({
        predicted_home_score: prediction.home_score,
        predicted_away_score: prediction.away_score,
        predicted_advancing_team_id: prediction.advancing_team_id,
      })
    : null;

  let saveTimer = null;

  function scheduleSave() {
    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }

    const payload = readPredictionPayload();
    if (!payload.isReady) {
      setSaveStatus(payload.message, "draft");
      return;
    }

    if (!isSaving && getPayloadFingerprint(payload) === lastSavedFingerprint) {
      setSaveStatus("Сохранено.", "saved");
      return;
    }

    setSaveStatus("Изменения будут сохранены…", "draft");
    saveTimer = window.setTimeout(() => {
      saveTimer = null;
      void savePrediction();
    }, 400);
  }

  async function savePrediction() {
    if (isSaving) {
      return;
    }

    const payload = readPredictionPayload();
    if (!payload.isReady) {
      setSaveStatus(payload.message, "draft");
      return;
    }

    const fingerprint = getPayloadFingerprint(payload);
    if (fingerprint === lastSavedFingerprint) {
      setSaveStatus("Сохранено.", "saved");
      return;
    }

    isSaving = true;
    setSaveStatus("Сохраняем…", "saving");

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/matches/${match.id}/prediction`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!result || !result.prediction) {
        throw new Error("Сервер вернул некорректный ответ при сохранении прогноза.");
      }

      match.prediction = result.prediction;
      lastSavedFingerprint = fingerprint;
    } catch (error) {
      lastSavedFingerprint = null;
      isSaving = false;
      const currentPayload = readPredictionPayload();
      if (
        currentPayload.isReady &&
        getPayloadFingerprint(currentPayload) !== fingerprint
      ) {
        void savePrediction();
        return;
      }
      if (!currentPayload.isReady) {
        setSaveStatus(currentPayload.message, "draft");
        return;
      }

      const errorMessage =
        error instanceof Error ? error.message : "Не удалось сохранить прогноз.";
      setSaveStatus(errorMessage, "error");
      return;
    }

    isSaving = false;
    const currentPayload = readPredictionPayload();
    if (!currentPayload.isReady) {
      setSaveStatus(currentPayload.message, "draft");
      return;
    }
    if (getPayloadFingerprint(currentPayload) !== lastSavedFingerprint) {
      void savePrediction();
      return;
    }

    setSaveStatus("Сохранено.", "saved");
  }

  homeScoreField.append(homeScoreLabel, homeScoreInput);
  awayScoreField.append(awayScoreLabel, awayScoreInput);
  scoreGrid.append(homeScoreField, awayScoreField);
  actions.append(retryButton);
  form.append(
    scoreHeading,
    scoreGrid,
    advancingTeamField.element,
    saveStatus,
    actions,
  );
  section.append(heading, hint, form);

  retryButton.addEventListener("click", () => {
    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }
    void savePrediction();
  });
  homeScoreInput.addEventListener("input", scheduleSave);
  awayScoreInput.addEventListener("input", scheduleSave);
  advancingTeamField.element.addEventListener("change", scheduleSave);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }
    void savePrediction();
  });

  if (prediction) {
    setSaveStatus("Сохранено.", "saved");
  } else {
    setSaveStatus("", "draft");
  }

  Promise.resolve().then(() => {
    ensurePredictionProgressSummary(section);
  });

  return section;
}


function getSwissStagePrediction(contest) {
  const prediction = contest?.swiss_stage_prediction;

  if (!prediction || typeof prediction !== "object") {
    return {
      is_enabled: false,
      deadline_at: null,
      direct_qualifier_count: 4,
      elimination_qualifier_count: 4,
      candidates: [],
      prediction: null,
      actual_result: null,
      is_open: false,
      settings_locked: false,
      awarded_points: null,
      awards: [],
    };
  }

  return {
    is_enabled: prediction.is_enabled === true,
    deadline_at: typeof prediction.deadline_at === "string"
      ? prediction.deadline_at
      : null,
    direct_qualifier_count: Number.isSafeInteger(
      prediction.direct_qualifier_count,
    )
      ? prediction.direct_qualifier_count
      : 4,
    elimination_qualifier_count: Number.isSafeInteger(
      prediction.elimination_qualifier_count,
    )
      ? prediction.elimination_qualifier_count
      : 4,
    candidates: Array.isArray(prediction.candidates)
      ? prediction.candidates
      : [],
    prediction: prediction.prediction || null,
    actual_result: prediction.actual_result || null,
    is_open: prediction.is_open === true,
    settings_locked: prediction.settings_locked === true,
    awarded_points: Number.isSafeInteger(prediction.awarded_points)
      ? prediction.awarded_points
      : null,
    awards: Array.isArray(prediction.awards) ? prediction.awards : [],
  };
}

function getSwissStageSelectionIds(selection, key) {
  const teams = Array.isArray(selection?.[key]) ? selection[key] : [];
  return new Set(
    teams
      .map((team) => team?.id)
      .filter((teamId) => Number.isSafeInteger(teamId) && teamId > 0),
  );
}

function createSwissStageStatus(prediction) {
  const status = createElement("span", {
    className: "champion-card-status",
  });
  if (!prediction.is_enabled) {
    status.textContent = "Выключен";
    status.classList.add("champion-card-status--disabled");
  } else if (prediction.actual_result) {
    status.textContent = "Итоги внесены";
    status.classList.add("champion-card-status--completed");
  } else if (prediction.is_open) {
    status.textContent = "Открыт";
    status.classList.add("champion-card-status--open");
  } else {
    status.textContent = "Закрыт";
    status.classList.add("champion-card-status--closed");
  }
  return status;
}

function createSwissStageMeta(prediction) {
  const meta = createElement("div", {
    className: "champion-card-meta",
  });
  meta.append(
    createElement("p", {
      className: "match-meta",
      text: prediction.deadline_at
        ? `Дедлайн: ${formatMatchStartsAt(prediction.deadline_at)}`
        : "Дедлайн не задан.",
    }),
    createElement("p", {
      className: "match-meta",
      text: (
        `Напрямую: ${prediction.direct_qualifier_count}; ` +
        `через элиминейшн-раунд: ` +
        `${prediction.elimination_qualifier_count}.`
      ),
    }),
  );
  return meta;
}

function createSwissStageTeamSelector(
  prediction,
  initialSelection,
  {
    submitLabel,
    endpoint,
    onSaved,
    confirmCorrection = false,
  },
) {
  const form = createElement("form", {
    className: "swiss-stage-selection-form",
  });
  const progress = createElement("div", {
    className: "swiss-stage-progress",
  });
  const directProgress = createElement("strong");
  const eliminationProgress = createElement("strong");
  const list = createElement("ul", {
    className: "swiss-stage-team-list",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    submitLabel,
    "primary-action-button",
    "submit",
  );
  const directIds = getSwissStageSelectionIds(
    initialSelection,
    "direct_teams",
  );
  const eliminationIds = getSwissStageSelectionIds(
    initialSelection,
    "elimination_teams",
  );

  function setCategory(teamId, category) {
    if (category === "direct") {
      if (directIds.has(teamId)) {
        directIds.delete(teamId);
      } else if (directIds.size < prediction.direct_qualifier_count) {
        eliminationIds.delete(teamId);
        directIds.add(teamId);
      }
    } else if (eliminationIds.has(teamId)) {
      eliminationIds.delete(teamId);
    } else if (
      eliminationIds.size < prediction.elimination_qualifier_count
    ) {
      directIds.delete(teamId);
      eliminationIds.add(teamId);
    }
    sync();
  }

  function sync() {
    directProgress.textContent = (
      `Пройдут напрямую: ${directIds.size} из ` +
      `${prediction.direct_qualifier_count}`
    );
    eliminationProgress.textContent = (
      `Через элиминейшн-раунд: ${eliminationIds.size} из ` +
      `${prediction.elimination_qualifier_count}`
    );
    submitButton.disabled = (
      directIds.size !== prediction.direct_qualifier_count ||
      eliminationIds.size !== prediction.elimination_qualifier_count
    );
    for (const row of list.children) {
      const teamId = Number(row.dataset.teamId);
      const directButton = row.querySelector("[data-category='direct']");
      const eliminationButton = row.querySelector(
        "[data-category='elimination']",
      );
      directButton.classList.toggle("is-selected", directIds.has(teamId));
      eliminationButton.classList.toggle(
        "is-selected",
        eliminationIds.has(teamId),
      );
      directButton.setAttribute(
        "aria-pressed",
        directIds.has(teamId) ? "true" : "false",
      );
      eliminationButton.setAttribute(
        "aria-pressed",
        eliminationIds.has(teamId) ? "true" : "false",
      );
    }
  }

  for (const team of prediction.candidates) {
    const row = createElement("li", {
      className: "swiss-stage-team-row",
    });
    row.dataset.teamId = String(team.id);
    const name = createElement("span", {
      className: "swiss-stage-team-name",
      text: team.name,
    });
    const choices = createElement("div", {
      className: "swiss-stage-team-actions",
    });
    const directButton = createActionButton(
      "Напрямую",
      "swiss-stage-choice",
    );
    const eliminationButton = createActionButton(
      "Элиминейшн",
      "swiss-stage-choice",
    );
    directButton.type = "button";
    eliminationButton.type = "button";
    directButton.dataset.category = "direct";
    eliminationButton.dataset.category = "elimination";
    directButton.addEventListener("click", () => {
      setCategory(team.id, "direct");
    });
    eliminationButton.addEventListener("click", () => {
      setCategory(team.id, "elimination");
    });
    choices.append(directButton, eliminationButton);
    row.append(name, choices);
    list.append(row);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitButton.disabled) {
      return;
    }
    if (
      confirmCorrection &&
      !window.confirm(
        "Исправить итоги? Рейтинг будет пересчитан сразу.",
      )
    ) {
      return;
    }
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";
    try {
      const result = await apiRequest(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          direct_team_ids: [...directIds],
          elimination_team_ids: [...eliminationIds],
        }),
      });
      if (!result?.swiss_stage_prediction) {
        throw new Error("Сервер вернул некорректный ответ.");
      }
      onSaved(result.swiss_stage_prediction);
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error
          ? error.message
          : "Не удалось сохранить данные швейцарского этапа.",
        "error",
      );
      submitButton.textContent = submitLabel;
      sync();
    }
  });

  progress.append(directProgress, eliminationProgress);
  actions.append(submitButton);
  form.append(progress, list, message, actions);
  sync();
  return form;
}

function createSwissStageReadonlySelection(title, selection) {
  const section = createElement("div", {
    className: "swiss-stage-readonly",
  });
  section.append(
    createElement("strong", { text: title }),
    createElement("p", {
      text: (
        "Напрямую: " +
        (selection?.direct_teams?.map((team) => team.name).join(", ") || "—")
      ),
    }),
    createElement("p", {
      text: (
        "Через элиминейшн-раунд: " +
        (
          selection?.elimination_teams
            ?.map((team) => team.name)
            .join(", ") || "—"
        )
      ),
    }),
  );
  return section;
}

function getSwissStageCategoryLabel(category, mode = "actual") {
  if (category === "direct") {
    return mode === "predicted" ? "пройдёт напрямую" : "прошла напрямую";
  }
  if (category === "elimination") {
    return mode === "predicted"
      ? "пройдёт через элиминейшн-раунд"
      : "прошла через элиминейшн-раунд";
  }
  return "не прошла";
}

function formatSwissStageAwardPoints(points) {
  if (!Number.isSafeInteger(points)) {
    return "—";
  }
  return points > 0 ? `+${points}` : "0";
}

function createSwissStageAwardsBreakdown(awards) {
  const section = createElement("div", {
    className: "swiss-stage-awards",
  });
  section.append(
    createElement("strong", {
      className: "swiss-stage-awards-title",
      text: "Разбивка баллов",
    }),
  );
  const list = createElement("ul", {
    className: "swiss-stage-awards-list",
  });
  for (const award of awards) {
    const item = createElement("li", {
      className: "swiss-stage-award-row",
    });
    const details = createElement("div", {
      className: "swiss-stage-award-details",
    });
    details.append(
      createElement("strong", {
        className: "swiss-stage-award-team",
        text: award?.team?.name || "Команда",
      }),
      createElement("span", {
        text: (
          `Прогноз: ${getSwissStageCategoryLabel(
            award?.predicted_category,
            "predicted",
          )}`
        ),
      }),
      createElement("span", {
        text: (
          `Факт: ${getSwissStageCategoryLabel(award?.actual_category)}`
        ),
      }),
    );
    item.append(
      details,
      createElement("strong", {
        className: "swiss-stage-award-points",
        text: formatSwissStageAwardPoints(award?.points),
      }),
    );
    list.append(item);
  }
  section.append(list);
  return section;
}

function createSwissStagePredictionCard(contest, onUpdated) {
  const prediction = getSwissStagePrediction(contest);
  if (!prediction.is_enabled) {
    return null;
  }
  const item = createElement("li", {
    className: "match-list-item champion-card swiss-stage-card",
  });
  const header = createElement("div", {
    className: "match-card-header",
  });
  header.append(
    createElement("strong", {
      className: "match-teams",
      text: "Итоги швейцарского этапа",
    }),
    createSwissStageStatus(prediction),
  );
  item.append(header, createSwissStageMeta(prediction));
  if (prediction.actual_result) {
    item.append(
      createSwissStageReadonlySelection(
        "Фактический результат",
        prediction.actual_result,
      ),
    );
  }
  if (!prediction.is_open || contest.is_active === false) {
    item.append(
      prediction.prediction
        ? createSwissStageReadonlySelection(
          "Ваш прогноз",
          prediction.prediction,
        )
        : createElement("p", {
          className: "match-prediction-closed",
          text: "Прогноз не был сохранён.",
        }),
    );
    if (Number.isSafeInteger(prediction.awarded_points)) {
      item.append(
        createElement("p", {
          className: prediction.awarded_points > 0
            ? "champion-award champion-award--success"
            : "champion-award",
          text: `Начислено: ${prediction.awarded_points} ${
            getPointsLabel(prediction.awarded_points)
          }.`,
        }),
      );
    }
    if (
      prediction.actual_result &&
      Array.isArray(prediction.awards) &&
      prediction.awards.length > 0
    ) {
      item.append(createSwissStageAwardsBreakdown(prediction.awards));
    }
    return item;
  }
  item.append(
    createSwissStageTeamSelector(
      prediction,
      prediction.prediction,
      {
        submitLabel: prediction.prediction
          ? "Изменить прогноз"
          : "Сохранить прогноз",
        endpoint: (
          `/api/tma/contests/${contest.id}/swiss-stage-prediction`
        ),
        onSaved: () => onUpdated(),
      },
    ),
  );
  return item;
}

function getChampionPrediction(contest) {
  const championPrediction = contest?.champion_prediction;

  if (!championPrediction || typeof championPrediction !== "object") {
    return {
      is_enabled: false,
      deadline_at: null,
      points: 5,
      candidates: [],
      prediction: null,
      actual_champion: null,
      is_open: false,
      is_tournament_completed: false,
      awarded_points: null,
    };
  }

  return {
    is_enabled: championPrediction.is_enabled === true,
    deadline_at: typeof championPrediction.deadline_at === "string"
      ? championPrediction.deadline_at
      : null,
    points: Number.isSafeInteger(championPrediction.points)
      ? championPrediction.points
      : 5,
    candidates: Array.isArray(championPrediction.candidates)
      ? championPrediction.candidates
      : [],
    prediction: championPrediction.prediction || null,
    actual_champion: championPrediction.actual_champion || null,
    is_open: championPrediction.is_open === true,
    is_tournament_completed: championPrediction.is_tournament_completed === true,
    awarded_points: Number.isSafeInteger(championPrediction.awarded_points)
      ? championPrediction.awarded_points
      : null,
  };
}

function getMatchPredictionPublication(contest) {
  const publication = contest?.match_prediction_publication;

  return {
    is_enabled: publication?.is_enabled === true,
  };
}

function formatDateTimeLocalValue(utcValue) {
  // datetime-local expects the device's local wall-clock value, not UTC.
  if (typeof utcValue !== "string") {
    return "";
  }

  const date = new Date(utcValue);

  if (Number.isNaN(date.getTime())) {
    return "";
  }

  return formatLocalDateTime(date);
}

function formatLocalDateTime(date) {
  const year = String(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");

  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function getDefaultMatchStartsAtLocal(now = new Date()) {
  const startsAt = new Date(now.getTime());

  startsAt.setSeconds(0, 0);
  startsAt.setMinutes(startsAt.getMinutes() + 3);

  return formatLocalDateTime(startsAt);
}

function createChampionTeamSelect(
  candidates,
  {
    id,
    name,
    selectedTeamId = null,
    includeEmptyOption = true,
  } = {},
) {
  const select = createElement("select", {
    className: "text-input champion-team-select",
  });
  const normalizedSelectedTeamId = Number.isSafeInteger(selectedTeamId)
    ? selectedTeamId
    : null;

  select.id = id || "";
  select.name = name || "";

  if (includeEmptyOption) {
    const emptyOption = createElement("option", {
      text: "Выберите команду",
    });

    emptyOption.value = "";
    emptyOption.disabled = true;
    emptyOption.selected = normalizedSelectedTeamId === null;
    select.append(emptyOption);
  }

  for (const candidate of candidates) {
    if (
      !Number.isSafeInteger(candidate?.id) ||
      typeof candidate?.name !== "string" ||
      !candidate.name
    ) {
      continue;
    }

    const option = createElement("option", {
      text: candidate.name,
    });

    option.value = String(candidate.id);
    option.selected = candidate.id === normalizedSelectedTeamId;
    select.append(option);
  }

  return select;
}

function createChampionPredictionSettingsDisclosure(
  contest,
  championPrediction,
  onUpdated,
) {
  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryContent = createElement("div", {
    className: "match-form-summary-content",
  });
  const title = createElement("span", {
    className: "match-form-title",
    text: championPrediction.is_enabled
      ? "Настройки прогноза чемпиона"
      : "Настроить прогноз чемпиона",
  });
  const overview = createElement("span", {
    className: "match-form-overview",
    text: championPrediction.is_enabled
      ? "Срок и баллы"
      : "Включить прогноз",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: (
      "Укажите, до какого времени можно выбрать чемпиона и " +
      "сколько баллов принесёт верный прогноз."
    ),
  });
  const form = createElement("form", {
    className: "form-fields",
  });
  const enabledField = createElement("label", {
    className: "champion-enable-option",
  });
  const enabledInput = createElement("input");
  const enabledText = createElement("span", {
    text: "Включить прогноз на чемпиона",
  });
  const deadlineField = createElement("label", {
    className: "form-field",
  });
  const deadlineLabel = createElement("span", {
    className: "form-field-label",
    text: "Прогноз закрывается",
  });
  const deadlineInput = createElement("input", {
    className: "text-input",
  });
  const pointsField = createElement("label", {
    className: "form-field",
  });
  const pointsLabel = createElement("span", {
    className: "form-field-label",
    text: "Баллы за верный прогноз",
  });
  const pointsInput = createElement("input", {
    className: "text-input",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: (
      "После указанного времени участники больше не смогут " +
      "создать или изменить свой выбор."
    ),
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    championPrediction.is_enabled ? "Сохранить настройки" : "Включить прогноз",
    "primary-action-button",
    "submit",
  );

  disclosure.className = "match-form-disclosure champion-settings-disclosure";
  summaryContent.append(title, overview);
  summary.append(summaryContent);

  enabledInput.id = `contest-${contest.id}-champion-enabled`;
  enabledInput.name = `contest-${contest.id}-champion-enabled`;
  enabledInput.type = "checkbox";
  enabledInput.checked = championPrediction.is_enabled;
  enabledField.append(enabledInput, enabledText);

  deadlineInput.id = `contest-${contest.id}-champion-deadline`;
  deadlineInput.name = `contest-${contest.id}-champion-deadline`;
  deadlineInput.type = "datetime-local";
  deadlineInput.step = "60";
  deadlineInput.value = formatDateTimeLocalValue(
    championPrediction.deadline_at,
  );

  pointsInput.id = `contest-${contest.id}-champion-points`;
  pointsInput.name = `contest-${contest.id}-champion-points`;
  pointsInput.type = "number";
  pointsInput.min = "0";
  pointsInput.step = "1";
  pointsInput.inputMode = "numeric";
  pointsInput.value = String(championPrediction.points);

  function syncEnabledState() {
    const isEnabled = enabledInput.checked;

    deadlineInput.disabled = !isEnabled;
    deadlineInput.required = isEnabled;
    deadlineField.classList.toggle("is-disabled", !isEnabled);
    hint.hidden = !isEnabled;
    submitButton.textContent = isEnabled
      ? (
        championPrediction.is_enabled
          ? "Сохранить настройки"
          : "Включить прогноз"
      )
      : "Выключить прогноз";
  }

  syncEnabledState();

  enabledInput.addEventListener("change", syncEnabledState);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const enabled = enabledInput.checked;
    const pointsValue = pointsInput.value.trim();
    const points = Number(pointsValue);
    const deadlineAt = enabled ? new Date(deadlineInput.value) : null;

    if (
      !pointsValue ||
      !Number.isSafeInteger(points) ||
      points < 0
    ) {
      pointsInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Укажите целое неотрицательное количество баллов.",
        "error",
      );
      pointsInput.focus();
      return;
    }

    pointsInput.removeAttribute("aria-invalid");

    if (
      enabled &&
      (
        !deadlineInput.value ||
        deadlineAt === null ||
        Number.isNaN(deadlineAt.getTime())
      )
    ) {
      deadlineInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Укажите дату и время закрытия прогноза.",
        "error",
      );
      deadlineInput.focus();
      return;
    }

    deadlineInput.removeAttribute("aria-invalid");

    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/champion-prediction/settings`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            enabled,
            deadline_at: enabled ? deadlineAt.toISOString() : null,
            points,
          }),
        },
      );

      if (!result || !result.champion_prediction) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении настроек.",
        );
      }

      onUpdated();
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить настройки прогноза.";

      setFormMessage(message, errorMessage, "error");
      syncEnabledState();
      submitButton.disabled = false;
    }
  });

  deadlineField.append(deadlineLabel, deadlineInput);
  pointsField.append(pointsLabel, pointsInput);
  actions.append(submitButton);
  form.append(
    enabledField,
    deadlineField,
    pointsField,
    hint,
    message,
    actions,
  );
  disclosure.append(summary, description, form);

  return disclosure;
}

function createChampionPredictionChoiceSection(
  contest,
  championPrediction,
  onUpdated,
) {
  const section = createElement("div", {
    className: "champion-card-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Ваш прогноз",
  });
  const candidates = championPrediction.candidates;

  section.append(heading);

  if (candidates.length === 0) {
    section.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: "Добавьте хотя бы один матч, чтобы выбрать чемпиона.",
      }),
    );
    return section;
  }

  if (contest.is_active === false || !championPrediction.is_open) {
    const text = championPrediction.prediction
      ? `Ваш прогноз: ${championPrediction.prediction.name}.`
      : contest.is_active === false
        ? "Конкурс завершён. Прогноз на чемпиона не был сохранён."
        : "Вы не выбрали чемпиона до закрытия прогноза.";

    section.append(
      createElement("p", {
        className: "match-prediction-closed",
        text,
      }),
    );
    return section;
  }

  const hint = createElement("p", {
    className: "match-prediction-hint",
    text: championPrediction.prediction
      ? "До дедлайна можно изменить выбранную команду."
      : "Выберите команду, которая, по вашему мнению, станет чемпионом.",
  });
  const form = createElement("form", {
    className: "champion-choice-form",
  });
  const field = createElement("label", {
    className: "form-field",
  });
  const label = createElement("span", {
    className: "form-field-label",
    text: "Чемпион турнира",
  });
  const select = createChampionTeamSelect(candidates, {
    id: `contest-${contest.id}-champion-prediction`,
    name: `contest-${contest.id}-champion-prediction`,
    selectedTeamId: championPrediction.prediction?.id ?? null,
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    championPrediction.prediction ? "Изменить прогноз" : "Сохранить прогноз",
    "primary-action-button",
    "submit",
  );

  field.append(label, select);
  actions.append(submitButton);
  form.append(field, message, actions);
  section.append(hint, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const predictedTeamId = Number(select.value);

    if (!Number.isSafeInteger(predictedTeamId) || predictedTeamId <= 0) {
      select.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Выберите команду.", "error");
      select.focus();
      return;
    }

    select.removeAttribute("aria-invalid");
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/champion-prediction`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            predicted_team_id: predictedTeamId,
          }),
        },
      );

      if (!result || !result.prediction) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении прогноза.",
        );
      }

      onUpdated();
    } catch (error) {
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить прогноз на чемпиона.";

      setFormMessage(message, errorMessage, "error");
      submitButton.textContent = championPrediction.prediction
        ? "Изменить прогноз"
        : "Сохранить прогноз";
      submitButton.disabled = false;
    }
  });

  return section;
}

function createContestChampionSection(
  contest,
  championPrediction,
  onUpdated,
) {
  const section = createElement("div", {
    className: "champion-card-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Итог турнира",
  });
  const candidates = championPrediction.candidates;

  section.append(heading);

  if (
    !championPrediction.is_tournament_completed
    || championPrediction.is_open
  ) {
    section.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: (
          "Фактического чемпиона можно указать после завершения всех " +
          "матчей конкурса и закрытия прогнозов на чемпиона."
        ),
      }),
    );
    return section;
  }

  if (candidates.length === 0) {
    return section;
  }

  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryTitle = createElement("span", {
    className: "match-result-summary-title",
    text: championPrediction.actual_champion
      ? `Чемпион: ${championPrediction.actual_champion.name}`
      : "Указать чемпиона",
  });
  const summaryAction = createElement("span", {
    className: "match-result-summary-action",
    text: championPrediction.actual_champion ? "Изменить" : "Открыть",
  });
  const hint = createElement("p", {
    className: "match-prediction-hint",
    text: (
      "Выберите победителя турнира. При исправлении рейтинг " +
      "автоматически пересчитается."
    ),
  });
  const form = createElement("form", {
    className: "champion-choice-form",
  });
  const field = createElement("label", {
    className: "form-field",
  });
  const label = createElement("span", {
    className: "form-field-label",
    text: "Фактический чемпион",
  });
  const select = createChampionTeamSelect(candidates, {
    id: `contest-${contest.id}-actual-champion`,
    name: `contest-${contest.id}-actual-champion`,
    selectedTeamId: championPrediction.actual_champion?.id ?? null,
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    championPrediction.actual_champion
      ? "Сохранить изменения"
      : "Сохранить чемпиона",
    "primary-action-button",
    "submit",
  );

  disclosure.className = "match-result-disclosure champion-result-disclosure";
  summary.append(summaryTitle, summaryAction);
  field.append(label, select);
  actions.append(submitButton);
  form.append(field, message, actions);
  disclosure.append(summary, hint, form);
  section.append(disclosure);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const championTeamId = Number(select.value);

    if (!Number.isSafeInteger(championTeamId) || championTeamId <= 0) {
      select.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Выберите фактического чемпиона.", "error");
      select.focus();
      return;
    }

    select.removeAttribute("aria-invalid");
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/champion`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            champion_team_id: championTeamId,
          }),
        },
      );

      if (!result || !result.champion) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении чемпиона.",
        );
      }

      onUpdated();
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить фактического чемпиона.";

      setFormMessage(message, errorMessage, "error");
      submitButton.textContent = championPrediction.actual_champion
        ? "Сохранить изменения"
        : "Сохранить чемпиона";
      submitButton.disabled = false;
    }
  });

  return section;
}

function createChampionCardStatus(championPrediction) {
  const status = createElement("span", {
    className: "champion-card-status",
  });

  if (!championPrediction.is_enabled) {
    status.textContent = "Не настроен";
    status.classList.add("champion-card-status--disabled");
  } else if (championPrediction.actual_champion) {
    status.textContent = "Завершён";
    status.classList.add("champion-card-status--completed");
  } else if (championPrediction.is_open) {
    status.textContent = "Открыт";
    status.classList.add("champion-card-status--open");
  } else {
    status.textContent = "Закрыт";
    status.classList.add("champion-card-status--closed");
  }

  return status;
}

function createChampionPredictionMeta(championPrediction) {
  const meta = createElement("div", {
    className: "champion-card-meta",
  });

  meta.append(
    createElement("p", {
      className: "match-meta",
      text: championPrediction.deadline_at
        ? `Прогноз закрывается: ${formatMatchStartsAt(championPrediction.deadline_at)}`
        : "Дедлайн прогноза не задан.",
    }),
    createElement("p", {
      className: "match-meta",
      text: (
        `За верный прогноз: +${championPrediction.points} ` +
        `${getPointsLabel(championPrediction.points)}.`
      ),
    }),
  );

  return meta;
}

function createMatchPredictionPublicationStatus(publication) {
  const status = createElement("span", {
    className: "champion-card-status match-prediction-publication-status",
    text: publication.is_enabled ? "Включена" : "Выключена",
  });

  status.classList.add(
    publication.is_enabled
      ? "champion-card-status--open"
      : "champion-card-status--disabled",
  );
  return status;
}

function createMatchPredictionPublicationSettingsDisclosure(
  contest,
  publication,
  onUpdated,
) {
  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryContent = createElement("div", {
    className: "match-form-summary-content",
  });
  const title = createElement("span", {
    className: "match-form-title",
    text: publication.is_enabled
      ? "Настройки публикации прогнозов и результатов"
      : "Настроить публикацию прогнозов и результатов",
  });
  const overview = createElement("span", {
    className: "match-form-overview",
    text: publication.is_enabled ? "Публикуется в чат" : "Выключена",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: (
      "Бот опубликует прогнозы после начала матча, результаты после их "
      + "внесения, прогнозы на чемпиона после закрытия приёма, а результаты "
      + "конкурса — после его завершения."
    ),
  });
  const form = createElement("form", {
    className: "form-fields",
  });
  const enabledField = createElement("label", {
    className: "champion-enable-option",
  });
  const enabledInput = createElement("input");
  const enabledText = createElement("span", {
    text: "Публиковать прогнозы и результаты в чат",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: (
      "Исторические события не публикуются. После включения сообщения "
      + "будут создаваться только для новых событий конкурса."
    ),
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    publication.is_enabled ? "Выключить публикацию" : "Включить публикацию",
    "primary-action-button",
    "submit",
  );

  disclosure.className = (
    "match-form-disclosure match-prediction-publication-settings-disclosure"
  );
  summaryContent.append(title, overview);
  summary.append(summaryContent);

  enabledInput.id = `contest-${contest.id}-match-prediction-publication-enabled`;
  enabledInput.name = (
    `contest-${contest.id}-match-prediction-publication-enabled`
  );
  enabledInput.type = "checkbox";
  enabledInput.checked = publication.is_enabled;
  enabledField.append(enabledInput, enabledText);

  function syncEnabledState() {
    if (enabledInput.checked === publication.is_enabled) {
      submitButton.textContent = "Сохранить настройки";
      return;
    }

    submitButton.textContent = enabledInput.checked
      ? "Включить публикацию"
      : "Выключить публикацию";
  }

  syncEnabledState();
  enabledInput.addEventListener("change", syncEnabledState);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/match-prediction-publication/settings`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            enabled: enabledInput.checked,
          }),
        },
      );

      if (!result || !result.match_prediction_publication) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении настройки.",
        );
      }

      onUpdated();
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить настройку публикации прогнозов.";

      setFormMessage(message, errorMessage, "error");
      syncEnabledState();
      submitButton.disabled = false;
    }
  });

  actions.append(submitButton);
  form.append(enabledField, hint, message, actions);
  disclosure.append(summary, description, form);

  return disclosure;
}

function createMatchPredictionPublicationAdministrationCard(contest, onUpdated) {
  const publication = getMatchPredictionPublication(contest);
  const item = createElement("li", {
    className: "match-list-item match-prediction-publication-card",
  });
  const header = createElement("div", {
    className: "match-card-header",
  });
  const title = createElement("strong", {
    className: "match-teams",
    text: "Публикация прогнозов в чате",
  });

  header.append(title, createMatchPredictionPublicationStatus(publication));
  item.append(
    header,
    createElement("p", {
      className: "match-prediction-closed",
      text: publication.is_enabled
        ? (
          "При начале матча бот раскроет в чате все сохранённые "
          + "прогнозы участников."
        )
        : "Публикация прогнозов при начале матча выключена.",
    }),
    createMatchPredictionPublicationSettingsDisclosure(
      contest,
      publication,
      onUpdated,
    ),
  );

  return item;
}

function createSwissStageSettingsForm(contest, prediction, onUpdated) {
  if (prediction.settings_locked) {
    return createElement("p", {
      className: "match-prediction-closed",
      text: (
        "Настройки зафиксированы после сохранения первого пользовательского " +
        "прогноза или фактического результата."
      ),
    });
  }
  const disclosure = document.createElement("details");
  disclosure.className = "match-form-disclosure champion-settings-disclosure";
  const summary = document.createElement("summary");
  summary.textContent = prediction.is_enabled
    ? "Изменить настройки"
    : "Включить прогноз";
  const form = createElement("form", { className: "form-fields" });
  const enabledField = createElement("label", {
    className: "champion-enable-option",
  });
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = prediction.is_enabled;
  enabledField.append(
    enabledInput,
    createElement("span", { text: "Прогноз включён" }),
  );
  const deadlineField = createElement("label", { className: "form-field" });
  const deadlineInput = document.createElement("input");
  deadlineInput.className = "text-input";
  deadlineInput.type = "datetime-local";
  deadlineInput.step = "60";
  deadlineInput.value = formatDateTimeLocalValue(prediction.deadline_at);
  deadlineField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Дедлайн",
    }),
    deadlineInput,
  );
  const limits = createElement("div", {
    className: "swiss-stage-settings-grid",
  });
  const directField = createElement("label", { className: "form-field" });
  const directInput = document.createElement("input");
  directInput.className = "text-input";
  directInput.type = "number";
  directInput.min = "1";
  directInput.step = "1";
  directInput.value = String(prediction.direct_qualifier_count);
  directField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Напрямую",
    }),
    directInput,
  );
  const eliminationField = createElement("label", {
    className: "form-field",
  });
  const eliminationInput = document.createElement("input");
  eliminationInput.className = "text-input";
  eliminationInput.type = "number";
  eliminationInput.min = "1";
  eliminationInput.step = "1";
  eliminationInput.value = String(prediction.elimination_qualifier_count);
  eliminationField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Через элиминейшн",
    }),
    eliminationInput,
  );
  limits.append(directField, eliminationField);
  const teamsField = createElement("label", { className: "form-field" });
  const teamsInput = document.createElement("textarea");
  teamsInput.className = "text-input swiss-stage-teams-input";
  teamsInput.rows = 8;
  teamsInput.placeholder = "По одной команде в строке";
  teamsInput.value = prediction.candidates
    .map((team) => team.name)
    .join("\n");
  teamsField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Команды этапа",
    }),
    teamsInput,
  );
  const message = createElement("p", { className: "form-message" });
  const actions = createElement("div", { className: "form-actions" });
  const submitButton = createActionButton(
    "Сохранить настройки",
    "primary-action-button",
    "submit",
  );
  actions.append(submitButton);
  form.append(
    enabledField,
    deadlineField,
    limits,
    teamsField,
    createElement("p", {
      className: "form-hint",
      text: "Укажите по одной команде в строке.",
    }),
    message,
    actions,
  );
  disclosure.append(summary, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const directCount = Number(directInput.value);
    const eliminationCount = Number(eliminationInput.value);
    const deadline = new Date(deadlineInput.value);
    const teamNames = teamsInput.value
      .split(/\r?\n/)
      .map((name) => normalizeTeamName(name))
      .filter(Boolean);
    if (
      !Number.isSafeInteger(directCount) ||
      directCount <= 0 ||
      !Number.isSafeInteger(eliminationCount) ||
      eliminationCount <= 0
    ) {
      setFormMessage(message, "Оба лимита должны быть положительными.", "error");
      return;
    }
    if (enabledInput.checked && Number.isNaN(deadline.getTime())) {
      setFormMessage(message, "Укажите корректный дедлайн.", "error");
      return;
    }
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";
    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/swiss-stage-prediction/settings`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: enabledInput.checked,
            deadline_at: deadlineInput.value ? deadline.toISOString() : null,
            direct_qualifier_count: directCount,
            elimination_qualifier_count: eliminationCount,
            team_names: teamNames,
          }),
        },
      );
      if (!result?.swiss_stage_prediction) {
        throw new Error("Сервер вернул некорректный ответ.");
      }
      onUpdated();
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error
          ? error.message
          : "Не удалось сохранить настройки.",
        "error",
      );
      submitButton.disabled = false;
      submitButton.textContent = "Сохранить настройки";
    }
  });
  return disclosure;
}

function createSwissStageAdministrationCard(contest, onUpdated) {
  const prediction = getSwissStagePrediction(contest);
  const item = createElement("li", {
    className: "match-list-item champion-card swiss-stage-card",
  });
  const header = createElement("div", { className: "match-card-header" });
  header.append(
    createElement("strong", {
      className: "match-teams",
      text: "Итоги швейцарского этапа",
    }),
    createSwissStageStatus(prediction),
  );
  item.append(header);
  if (prediction.is_enabled) {
    item.append(createSwissStageMeta(prediction));
  } else {
    item.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: "Настройте команды, дедлайн и количество проходящих команд.",
      }),
    );
  }
  item.append(createSwissStageSettingsForm(contest, prediction, onUpdated));

  if (prediction.is_enabled && !prediction.is_open) {
    if (prediction.actual_result) {
      item.append(
        createSwissStageReadonlySelection(
          "Сохранённый результат",
          prediction.actual_result,
        ),
      );
    }
    item.append(
      createSwissStageTeamSelector(
        prediction,
        prediction.actual_result,
        {
          submitLabel: prediction.actual_result
            ? "Исправить результат"
            : "Сохранить результат",
          endpoint: `/api/tma/contests/${contest.id}/swiss-stage-result`,
          onSaved: () => onUpdated(),
          confirmCorrection: Boolean(prediction.actual_result),
        },
      ),
    );
  } else if (prediction.is_enabled) {
    item.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: "Фактический результат можно внести после дедлайна.",
      }),
    );
  }
  return item;
}

function createChampionAdministrationCard(contest, state, onUpdated) {
  const championPrediction = getChampionPrediction(contest);
  const item = createElement("li", {
    className: "match-list-item champion-card champion-admin-card",
  });
  const header = createElement("div", {
    className: "match-card-header",
  });
  const title = createElement("strong", {
    className: "match-teams",
    text: "Прогноз на чемпиона",
  });

  header.append(title, createChampionCardStatus(championPrediction));
  item.append(header);

  if (!championPrediction.is_enabled) {
    item.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: (
          "Включите прогноз, задайте дедлайн и количество баллов. " +
          "Участники будут выбирать чемпиона на вкладке «Прогнозы»."
        ),
      }),
      createChampionPredictionSettingsDisclosure(
        contest,
        championPrediction,
        onUpdated,
      ),
    );
    return item;
  }

  item.append(
    createChampionPredictionMeta(championPrediction),
    championPrediction.actual_champion
      ? createElement("p", {
        className: "match-prediction-closed",
        text: (
          "Настройки зафиксированы после указания фактического чемпиона."
        ),
      })
      : createChampionPredictionSettingsDisclosure(
        contest,
        championPrediction,
        onUpdated,
      ),
    createContestChampionSection(
      contest,
      championPrediction,
      onUpdated,
    ),
  );

  return item;
}

function createChampionPredictionCard(contest, onUpdated) {
  const championPrediction = getChampionPrediction(contest);

  if (!championPrediction.is_enabled) {
    return null;
  }

  const item = createElement("li", {
    className: "match-list-item champion-card",
  });
  const header = createElement("div", {
    className: "match-card-header",
  });
  const title = createElement("strong", {
    className: "match-teams",
    text: "Чемпион турнира",
  });

  header.append(title, createChampionCardStatus(championPrediction));
  item.append(header, createChampionPredictionMeta(championPrediction));

  if (championPrediction.actual_champion) {
    item.append(
      createElement("p", {
        className: "champion-award",
        text: `Фактический чемпион: ${championPrediction.actual_champion.name}.`,
      }),
    );
  }

  if (championPrediction.actual_champion && championPrediction.prediction) {
    item.append(
      createElement("p", {
        className: championPrediction.awarded_points > 0
          ? "champion-award champion-award--success"
          : "champion-award",
        text: championPrediction.awarded_points > 0
          ? (
            `Ваш прогноз: ${championPrediction.prediction.name}. ` +
            `Начислено: +${championPrediction.awarded_points} ` +
            `${getPointsLabel(championPrediction.awarded_points)}.`
          )
          : `Ваш прогноз: ${championPrediction.prediction.name}.`,
      }),
    );
  }

  item.append(
    createChampionPredictionChoiceSection(
      contest,
      championPrediction,
      onUpdated,
    ),
  );

  return item;
}

function getPredictionSortTime(value) {
  if (typeof value !== "string") {
    return Number.POSITIVE_INFINITY;
  }

  const date = new Date(value);

  return Number.isNaN(date.getTime())
    ? Number.POSITIVE_INFINITY
    : date.getTime();
}

function comparePredictionListItems(left, right) {
  if (left.isOpen !== right.isOpen) {
    return left.isOpen ? -1 : 1;
  }

  if (left.sortTime !== right.sortTime) {
    return left.sortTime < right.sortTime ? -1 : 1;
  }

  return left.sortKey.localeCompare(right.sortKey);
}

function createMatchDeletionSection(
  contest,
  match,
  state,
  onMatchDeletionStateChange,
) {
  const section = createElement("div", {
    className: "match-deletion-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Удаление матча",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const matchName = `${match.home_team_name} — ${match.away_team_name}`;
  const isConfirming = state.deleteMatchId === match.id;

  setFormMessage(
    message,
    isConfirming ? state.deleteMatchMessage || "" : "",
    isConfirming ? state.deleteMatchMessageType || "" : "",
  );

  if (!isConfirming) {
    const description = createElement("p", {
      className: "match-prediction-closed",
      text: "Удаляйте матч, только если он был создан по ошибке.",
    });
    const continueButton = createActionButton(
      "Удалить матч",
      "danger-action-button",
    );

    continueButton.addEventListener("click", () => {
      onMatchDeletionStateChange({
        deleteMatchId: match.id,
        deleteMatchMessage: "",
        deleteMatchMessageType: "",
        matchesMessage: "",
        matchesMessageType: "",
      });
    });

    actions.append(continueButton);
    section.append(heading, description, message, actions);
    return section;
  }

  const panel = createElement("div", {
    className: "confirmation-panel match-deletion-confirmation",
  });
  const summary = createElement("p");
  const matchNameElement = createElement("strong", {
    text: matchName,
  });
  const details = createElement("p", {
    className: "form-hint",
    text: (
      "Вместе с матчем будут безвозвратно удалены результат, "
      + "прогнозы участников и начисленные баллы."
    ),
  });
  const cancelButton = createActionButton(
    "Отмена",
    "secondary-action-button",
  );
  const deleteButton = createActionButton(
    "Да, удалить матч",
    "danger-action-button",
  );

  summary.append("Удалить матч «", matchNameElement, "»?");
  panel.append(summary, details);
  actions.append(cancelButton, deleteButton);
  section.append(heading, panel, message, actions);

  cancelButton.addEventListener("click", () => {
    onMatchDeletionStateChange({
      deleteMatchId: null,
      deleteMatchMessage: "",
      deleteMatchMessageType: "",
    });
  });

  deleteButton.addEventListener("click", async () => {
    deleteButton.disabled = true;
    cancelButton.disabled = true;
    deleteButton.textContent = "Удаляем…";

    try {
      await apiRequest(
        `/api/tma/contests/${contest.id}/matches/${match.id}`,
        {
          method: "DELETE",
        },
      );

      onMatchDeletionStateChange({
        deleteMatchId: null,
        deleteMatchMessage: "",
        deleteMatchMessageType: "",
        matchesMessage: `Матч «${matchName}» удалён.`,
        matchesMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось удалить матч.";

      onMatchDeletionStateChange({
        deleteMatchId: match.id,
        deleteMatchMessage: errorMessage,
        deleteMatchMessageType: "error",
      });
    }
  });

  return section;
}

function createMatchStartEditingSection(
  contest,
  match,
  state,
  onMatchEditingStateChange,
) {
  const section = createElement("section", {
    className: "match-start-editing-section",
  });
  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const form = createElement("form", {
    className: "form-fields",
  });
  const field = createElement("label", {
    className: "form-field",
  });
  const label = createElement("span", {
    className: "form-field-label",
    text: "Новая дата и время начала",
  });
  const input = createElement("input", {
    className: "text-input",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: "Время указывается в вашем часовом поясе.",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    "Сохранить время",
    "primary-action-button",
    "submit",
  );
  const isCurrentMatch = state.matchStartEditId === match.id;

  disclosure.className = "match-form-disclosure";
  disclosure.open = isCurrentMatch;
  summary.textContent = "Изменить дату и время";
  input.type = "datetime-local";
  input.step = "60";
  input.required = true;
  input.value = isCurrentMatch && state.matchStartEditValue
    ? state.matchStartEditValue
    : formatDateTimeLocalValue(match.starts_at_utc);

  setFormMessage(
    message,
    isCurrentMatch ? state.matchStartEditMessage || "" : "",
    isCurrentMatch ? state.matchStartEditMessageType || "" : "",
  );
  field.append(label, input);
  actions.append(submitButton);
  form.append(field, hint, message, actions);
  disclosure.append(summary, form);
  section.append(disclosure);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const startsAtLocal = input.value;
    const startsAt = new Date(startsAtLocal);

    if (
      !startsAtLocal
      || Number.isNaN(startsAt.getTime())
      || startsAt.getTime() <= Date.now()
    ) {
      input.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Новое время начала матча должно быть в будущем.",
        "error",
      );
      input.focus();
      return;
    }

    input.removeAttribute("aria-invalid");
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      await apiRequest(
        `/api/tma/contests/${contest.id}/matches/${match.id}`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            starts_at_utc: startsAt.toISOString(),
          }),
        },
      );
      onMatchEditingStateChange({
        matchStartEditId: null,
        matchStartEditValue: "",
        matchStartEditMessage: "",
        matchStartEditMessageType: "",
        matchesMessage: (
          `Время матча «${match.home_team_name} — ${match.away_team_name}» изменено.`
        ),
        matchesMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      onMatchEditingStateChange({
        matchStartEditId: match.id,
        matchStartEditValue: startsAtLocal,
        matchStartEditMessage: error instanceof Error
          ? error.message
          : "Не удалось изменить время начала матча.",
        matchStartEditMessageType: "error",
      });
    }
  });

  return section;
}

function createMatchListItem(
  contest,
  match,
  state,
  onResultSaved,
  onMatchDeletionStateChange,
  { showPredictions, showResults, canManageResults = true },
) {
  const item = createElement("li", {
    className: "match-list-item",
  });
  const header = createElement("div", {
    className: "match-card-header",
  });
  const teams = createElement("strong", {
    className: "match-teams",
    text: `${match.home_team_name} — ${match.away_team_name}`,
  });
  const status = createElement("span", {
    className: `match-status match-status--${match.status}`,
    text: getMatchStatusLabel(match.status),
  });
  const meta = createElement("div", {
    className: "match-card-meta",
  });
  const startsAt = createElement("p", {
    className: "match-meta",
    text: `Начало: ${formatMatchStartsAt(match.starts_at_utc)}`,
  });
  const startsInText = formatMatchStartsIn(match);
  const sections = [header, meta];

  header.append(teams, status);
  meta.append(startsAt);
  if (startsInText) {
    meta.append(
      createElement("p", {
        className: "match-meta match-starts-in",
        text: startsInText,
      }),
    );
  }

  if (
    showResults
    && contest.is_active !== false
    && onMatchDeletionStateChange
    && isMatchPredictionOpen(match)
  ) {
    sections.push(
      createMatchStartEditingSection(
        contest,
        match,
        state,
        onMatchDeletionStateChange,
      ),
    );
  }

  if (showPredictions) {
    sections.push(createMatchPredictionSection(contest, match));
  }

  if (showResults) {
    sections.push(
      createMatchResultSection(
        contest,
        match,
        state,
        onResultSaved,
        canManageResults,
      ),
    );
  }

  if (
    showResults
    && contest.is_active !== false
    && onMatchDeletionStateChange
  ) {
    sections.push(
      createMatchDeletionSection(
        contest,
        match,
        state,
        onMatchDeletionStateChange,
      ),
    );
  }

  item.append(...sections);
  return item;
}

function createPredictionListItems(contest, matches, onChampionUpdated) {
  const items = matches.map((match) => ({
    kind: "match",
    match,
    isOpen: isMatchPredictionOpen(match),
    sortTime: getPredictionSortTime(match.starts_at_utc),
    sortKey: `match-${String(match.id).padStart(12, "0")}`,
  }));
  const championPrediction = getChampionPrediction(contest);
  const swissStagePrediction = getSwissStagePrediction(contest);

  if (championPrediction.is_enabled) {
    items.push({
      kind: "champion",
      isOpen: championPrediction.is_open,
      sortTime: getPredictionSortTime(championPrediction.deadline_at),
      sortKey: "champion",
    });
  }
  if (swissStagePrediction.is_enabled) {
    items.push({
      kind: "swiss-stage",
      isOpen: swissStagePrediction.is_open,
      sortTime: getPredictionSortTime(swissStagePrediction.deadline_at),
      sortKey: "swiss-stage",
    });
  }

  return items
    .sort(comparePredictionListItems)
    .map((item) => {
      if (item.kind === "champion") {
        return createChampionPredictionCard(contest, onChampionUpdated);
      }
      if (item.kind === "swiss-stage") {
        return createSwissStagePredictionCard(contest, onChampionUpdated);
      }

      return createMatchListItem(contest, item.match, {}, null, null, {
        showPredictions: true,
        showResults: false,
      });
    })
    .filter(Boolean);
}

function createMatchesCard(
  contest,
  matches,
  state,
  onResultSaved,
  onMatchDeletionStateChange,
  {
    title,
    emptyMessages,
    showPredictions,
    showResults,
    canManageResults = true,
    leadingItems = [],
    listItems = null,
  },
) {
  const normalizedLeadingItems = Array.isArray(leadingItems)
    ? leadingItems.filter(Boolean)
    : [];
  const normalizedListItems = Array.isArray(listItems)
    ? listItems.filter(Boolean)
    : null;
  const visibleMessage = showResults ? state.matchesMessage || "" : "";
  const visibleMessageType = showResults
    ? state.matchesMessageType || ""
    : "";
  const hasListItems = normalizedListItems !== null
    ? normalizedListItems.length > 0
    : normalizedLeadingItems.length > 0 || matches.length > 0;

  if (matches.length === 0 && !hasListItems && !visibleMessage) {
    return createInfoCard(title, emptyMessages, "matches-card");
  }

  const card = createElement("section", {
    className: "info-card matches-card",
  });
  const heading = createElement("h2", {
    text: title,
  });
  const message = createElement("p", {
    className: "form-message",
  });

  setFormMessage(message, visibleMessage, visibleMessageType);
  card.append(heading, message);

  if (hasListItems) {
    const list = createElement("ol", {
      className: "match-list",
    });

    if (normalizedListItems !== null) {
      list.append(...normalizedListItems);
    } else {
      list.append(...normalizedLeadingItems);

      for (const match of matches) {
        list.append(
          createMatchListItem(
            contest,
            match,
            state,
            onResultSaved,
            onMatchDeletionStateChange,
            {
              showPredictions,
              showResults,
              canManageResults,
            },
          ),
        );
      }
    }

    card.append(list);
  }

  if (matches.length === 0) {
    for (const emptyMessage of emptyMessages) {
      card.append(
        createElement("p", {
          className: "subtitle",
          text: emptyMessage,
        }),
      );
    }
  }

  return card;
}

function createMatchFormCard(bootstrap, contest, state) {
  const draft = state.matchDraft || {};
  const card = createElement("section", {
    className: "info-card contest-form-card match-form-card",
  });
  const disclosure = document.createElement("details");
  const summary = document.createElement("summary");
  const summaryContent = createElement("div", {
    className: "match-form-summary-content",
  });
  const title = createElement("span", {
    className: "match-form-title",
    text: "Добавить матч",
  });
  const overview = createElement("span", {
    className: "match-form-overview",
    text: "Укажите команды и время начала",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Укажите команды и время начала матча.",
  });
  const form = createElement("form", {
    className: "form-fields",
  });
  const homeTeamField = createElement("label", {
    className: "form-field",
  });
  const homeTeamLabel = createElement("span", {
    className: "form-field-label",
    text: "Первая команда",
  });
  const homeTeamInput = createElement("input", {
    className: "text-input",
  });
  const awayTeamField = createElement("label", {
    className: "form-field",
  });
  const awayTeamLabel = createElement("span", {
    className: "form-field-label",
    text: "Вторая команда",
  });
  const awayTeamInput = createElement("input", {
    className: "text-input",
  });
  const startsAtField = createElement("label", {
    className: "form-field",
  });
  const startsAtLabel = createElement("span", {
    className: "form-field-label",
    text: "Дата и время начала",
  });
  const startsAtInput = createElement("input", {
    className: "text-input",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: "Время указывается в вашем часовом поясе.",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const submitButton = createActionButton(
    "Добавить матч",
    "primary-action-button",
    "submit",
  );

  disclosure.className = "match-form-disclosure";
  disclosure.open = Boolean(state.matchFormMessage || state.matchDraft);
  const wasInitiallyOpen = disclosure.open;
  summaryContent.append(title, overview);
  summary.append(summaryContent);

  homeTeamInput.id = "match-home-team-name";
  homeTeamInput.name = "match-home-team-name";
  homeTeamInput.type = "text";
  homeTeamInput.maxLength = CONTEST_NAME_MAX_LENGTH;
  homeTeamInput.autocomplete = "off";
  homeTeamInput.placeholder = "Например: Аргентина";
  homeTeamInput.value = draft.homeTeamName || "";
  homeTeamInput.required = true;

  awayTeamInput.id = "match-away-team-name";
  awayTeamInput.name = "match-away-team-name";
  awayTeamInput.type = "text";
  awayTeamInput.maxLength = CONTEST_NAME_MAX_LENGTH;
  awayTeamInput.autocomplete = "off";
  awayTeamInput.placeholder = "Например: Бразилия";
  awayTeamInput.value = draft.awayTeamName || "";
  awayTeamInput.required = true;

  startsAtInput.id = "match-starts-at";
  startsAtInput.name = "match-starts-at";
  startsAtInput.type = "datetime-local";
  startsAtInput.step = "60";
  startsAtInput.value =
    draft.startsAtLocal || getDefaultMatchStartsAtLocal();
  startsAtInput.required = true;

  setFormMessage(
    message,
    state.matchFormMessage || "",
    state.matchFormMessageType || "",
  );

  homeTeamField.append(homeTeamLabel, homeTeamInput);
  awayTeamField.append(awayTeamLabel, awayTeamInput);
  startsAtField.append(startsAtLabel, startsAtInput);
  actions.append(submitButton);
  form.append(
    homeTeamField,
    awayTeamField,
    startsAtField,
    hint,
    message,
    actions,
  );
  disclosure.append(summary, description, form);
  card.append(disclosure);

  disclosure.addEventListener("toggle", () => {
    if (
      disclosure.open
      && !wasInitiallyOpen
      && !state.matchDraft
    ) {
      homeTeamInput.focus();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    const homeTeamName = normalizeTeamName(homeTeamInput.value);
    const awayTeamName = normalizeTeamName(awayTeamInput.value);
    const startsAtLocal = startsAtInput.value;
    const startsAt = new Date(startsAtLocal);

    if (!homeTeamName) {
      homeTeamInput.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Введите название первой команды.", "error");
      homeTeamInput.focus();
      return;
    }

    homeTeamInput.removeAttribute("aria-invalid");

    if (!awayTeamName) {
      awayTeamInput.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Введите название второй команды.", "error");
      awayTeamInput.focus();
      return;
    }

    awayTeamInput.removeAttribute("aria-invalid");

    if (homeTeamName.toLocaleLowerCase() === awayTeamName.toLocaleLowerCase()) {
      homeTeamInput.setAttribute("aria-invalid", "true");
      awayTeamInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "В матче должны участвовать разные команды.",
        "error",
      );
      awayTeamInput.focus();
      return;
    }

    homeTeamInput.removeAttribute("aria-invalid");
    awayTeamInput.removeAttribute("aria-invalid");

    if (!startsAtLocal || Number.isNaN(startsAt.getTime())) {
      startsAtInput.setAttribute("aria-invalid", "true");
      setFormMessage(
        message,
        "Укажите корректную дату и время начала матча.",
        "error",
      );
      startsAtInput.focus();
      return;
    }

    startsAtInput.removeAttribute("aria-invalid");

    const matchDraft = {
      homeTeamName,
      awayTeamName,
      startsAtLocal,
    };
    const idempotencyKey =
      state.matchIdempotencyKey || createIdempotencyKey("match");

    submitButton.disabled = true;
    submitButton.textContent = "Добавляем…";

    try {
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/matches`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            [IDEMPOTENCY_KEY_HEADER]: idempotencyKey,
          },
          body: JSON.stringify({
            home_team_name: homeTeamName,
            away_team_name: awayTeamName,
            starts_at_utc: startsAt.toISOString(),
          }),
        },
      );

      if (!result || !result.match) {
        throw new Error("Сервер вернул некорректный ответ при создании матча.");
      }

      const matchName =
        `${result.match.home_team_name} — ${result.match.away_team_name}`;
      const successMessage = result.was_created
        ? `Матч «${matchName}» добавлен.`
        : `Матч «${matchName}» уже был добавлен ранее.`;
      const currentMatches = Array.isArray(contest.matches)
        ? contest.matches
        : [];
      const updatedContest = {
        ...contest,
        matches: [
          ...currentMatches.filter((match) => match.id !== result.match.id),
          result.match,
        ].sort((left, right) => {
          const startsAtDifference =
            new Date(left.starts_at_utc).getTime() -
            new Date(right.starts_at_utc).getTime();

          return startsAtDifference || left.id - right.id;
        }),
      };

      renderContestDetailsRoute(bootstrap, updatedContest, {
        ...state,
        activeTab: "matches",
        matchFormMessage: successMessage,
        matchFormMessageType: "success",
      });
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      const errorMessage =
        error instanceof Error ? error.message : "Не удалось добавить матч.";

      renderContestDetailsRoute(bootstrap, contest, {
        ...state,
        matchDraft,
        matchIdempotencyKey: idempotencyKey,
        matchFormMessage: errorMessage,
        matchFormMessageType: "error",
      });
    }
  });

  return card;
}

function renderContestDetailsLoading(bootstrap) {
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);

  setChatSummary(`Привет, ${userName}. Чат «${chatTitle}».`);
  appContentElement.replaceChildren(
    createStatusCard(
      "Открываем конкурс",
      "Загружаем матчи и настройки конкурса…",
    ),
  );
}

function renderContestDetailsError(bootstrap, message) {
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);
  const card = createInfoCard("Не удалось открыть конкурс", [message]);
  const actions = createElement("div", {
    className: "form-actions",
  });
  const backButton = createActionButton(
    "К списку конкурсов",
    "secondary-action-button",
  );

  backButton.addEventListener("click", () => {
    renderContestScreen(bootstrap);
  });

  actions.append(backButton);
  card.append(actions);

  setChatSummary(`Привет, ${userName}. Чат «${chatTitle}».`);
  appContentElement.replaceChildren(card);
}

function renderContestDetailsScreen(bootstrap, contest, state = {}) {
  currentViewMode = "participant";
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);
  const matches = Array.isArray(contest.matches) ? contest.matches : [];
  const leaderboard = Array.isArray(contest.leaderboard)
    ? contest.leaderboard
    : [];
  const activeTab = getActiveContestTab(state.activeTab);
  const isActive = contest.is_active !== false;
  const cards = [
    createContestDetailsCard(contest, () => {
      renderContestScreen(bootstrap);
    }),
    createContestTabs(activeTab, (nextTab) => {
      if (nextTab === activeTab) {
        return;
      }

      void openContest(bootstrap, contest.id, {
        ...state,
        activeTab: nextTab,
      });
    }),
  ];

  if (activeTab === "leaderboard") {
    cards.push(
      createLeaderboardCard(
        leaderboard,
        contest.champion_prediction,
        contest.swiss_stage_prediction,
      ),
    );
  } else if (activeTab === "matches") {
    cards.push(
      createMatchesCard(
        contest,
        matches,
        state,
        null,
        null,
        {
          title: "Матчи",
          emptyMessages: isActive
            ? ["Матчей пока нет."]
            : ["Матчей нет."],
          showPredictions: false,
          showResults: true,
          canManageResults: false,
          leadingItems: [],
        },
      ),
    );
  } else {
    cards.push(
      createContestRulesCard(
        contest.champion_prediction,
        contest.swiss_stage_prediction,
      ),
      createMatchesCard(
        contest,
        matches,
        state,
        null,
        null,
        {
          title: "Прогнозы",
          emptyMessages: isActive
            ? [
              "Матчей пока нет.",
              "Когда кто-то добавит матч, здесь можно будет сохранить прогноз.",
            ]
            : ["Матчей нет."],
          showPredictions: true,
          showResults: false,
          listItems: createPredictionListItems(
            contest,
            matches,
            () => {
              void openContest(bootstrap, contest.id, {
                ...state,
                activeTab: "predictions",
              });
            },
          ),
        },
      ),
    );
  }

  setChatSummary(`Привет, ${userName}. Чат «${chatTitle}».`);
  appContentElement.replaceChildren(...cards);
}

function renderContestDetailsRoute(bootstrap, contest, state = {}) {
  if (state.managementMode === true) {
    renderContestManagementScreen(bootstrap, contest, state);
    return;
  }
  renderContestDetailsScreen(bootstrap, contest, state);
}

function createContestManagementHeader(contest, bootstrap) {
  const card = createElement("section", {
    className: "contest-overview management-overview",
  });
  const backButton = createActionButton(
    "← К выбору конкурса",
    "contest-back-link",
  );
  const participantButton = createActionButton(
    "← Вернуться в конкурс",
    "secondary-action-button",
  );
  const mode = createElement("p", {
    className: "management-mode-label",
    text: "Режим управления",
  });
  const heading = createElement("h2", { text: contest.name });

  backButton.addEventListener("click", () => {
    void openManagement(bootstrap);
  });
  participantButton.addEventListener("click", () => {
    void openContest(bootstrap, contest.id);
  });
  card.append(backButton, mode, heading, participantButton);
  return card;
}

function renderContestManagementScreen(bootstrap, contest, state = {}) {
  currentViewMode = "management";
  setChatSummary();
  const matches = Array.isArray(contest.matches) ? contest.matches : [];
  const isActive = contest.is_active !== false;
  const managementState = {
    ...state,
    managementMode: true,
  };
  const cards = [
    createContestManagementHeader(contest, bootstrap),
    createMatchesCard(
      contest,
      matches,
      managementState,
      (resultState) => {
        void openContest(bootstrap, contest.id, {
          ...managementState,
          resultMatchId: resultState.matchId,
          resultMessage: resultState.message,
          resultMessageType: resultState.type,
        });
      },
      (deletionState) => {
        void openContest(bootstrap, contest.id, {
          ...managementState,
          ...deletionState,
        });
      },
      {
        title: "Управление матчами",
        emptyMessages: isActive
          ? ["Матчей пока нет.", "Добавьте первый матч ниже."]
          : ["Матчей нет."],
        showPredictions: false,
        showResults: true,
        canManageResults: isActive,
        leadingItems: isActive
          ? [
            createMatchPredictionPublicationAdministrationCard(
              contest,
              () => {
                void openContest(bootstrap, contest.id, managementState);
              },
            ),
            createChampionAdministrationCard(
              contest,
              managementState,
              () => {
                void openContest(bootstrap, contest.id, managementState);
              },
            ),
            createSwissStageAdministrationCard(
              contest,
              () => {
                void openContest(bootstrap, contest.id, managementState);
              },
            ),
          ]
          : [],
      },
    ),
  ];

  if (isActive) {
    cards.push(
      createMatchFormCard(bootstrap, contest, managementState),
      createContestCompletionCard(bootstrap, contest, managementState),
      createContestDeletionCard(bootstrap, contest, managementState),
    );
  }

  appContentElement.replaceChildren(...cards);
}

function renderContestManagementError(bootstrap, message) {
  currentViewMode = "management";
  setChatSummary();
  const card = createInfoCard(
    "Не удалось открыть управление конкурсом",
    [message],
    "management-error-card",
  );
  const actions = createElement("div", { className: "form-actions" });
  const backButton = createActionButton(
    "К выбору конкурса",
    "secondary-action-button",
  );
  backButton.addEventListener("click", () => {
    void openManagement(bootstrap);
  });
  actions.append(backButton);
  card.append(actions);
  appContentElement.replaceChildren(
    createManagementHeaderCard(bootstrap),
    card,
  );
}

async function openContest(bootstrap, contestId, state = {}) {
  renderContestDetailsLoading(bootstrap);

  try {
    const path = state.managementMode === true
      ? `/api/tma/management/contests/${contestId}`
      : `/api/tma/contests/${contestId}`;
    const result = await apiRequest(path);

    if (!result || !result.contest) {
      throw new Error("Сервер вернул некорректный ответ при открытии конкурса.");
    }

    if (state.managementMode === true) {
      renderContestManagementScreen(bootstrap, result.contest, state);
    } else {
      renderContestDetailsScreen(bootstrap, result.contest, state);
    }
  } catch (error) {
    if (
      state.managementMode === true
      && handleManagementRequestError(
        error,
        "Доступ к управлению этим конкурсом отсутствует.",
      )
    ) {
      return;
    }
    const errorMessage =
      error instanceof Error ? error.message : "Не удалось открыть конкурс.";

    if (state.managementMode === true) {
      renderContestManagementError(bootstrap, errorMessage);
    } else {
      renderContestDetailsError(bootstrap, errorMessage);
    }
  }
}

function getAuditEventPresentation(eventType) {
  return AUDIT_EVENT_PRESENTATIONS[eventType] || {
    label: "Действие",
    group: "Другие действия",
  };
}

function getAuditEventTitle(event) {
  if (event.event_type === "contest_updated") {
    const before = event.before_state || {};
    const after = event.after_state || {};
    const changedSection = event.metadata?.changed_section;
    if (
      changedSection === "match_prediction_publication"
      && before.match_prediction_publication_enabled === false
      && after.match_prediction_publication_enabled === true
    ) {
      return "Включена публикация прогнозов";
    }
    if (
      changedSection === "match_prediction_publication"
      && before.match_prediction_publication_enabled === true
      && after.match_prediction_publication_enabled === false
    ) {
      return "Выключена публикация прогнозов";
    }
  }
  return getAuditEventPresentation(event.event_type).label;
}

function formatAuditDateTime(value) {
  const normalizedValue = (
    typeof value === "string"
    && !/(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  )
    ? `${value.replace(" ", "T")}Z`
    : value;
  const date = new Date(normalizedValue);

  if (Number.isNaN(date.getTime())) {
    return value;
  }

  return new Intl.DateTimeFormat("ru-RU", {
    dateStyle: "long",
    timeStyle: "short",
  }).format(date);
}

function getAuditRoleLabel(role) {
  return AUDIT_ROLE_LABELS[role] || role || "Роль не указана";
}

function formatAuditUserIdentity(user, telegramUserId) {
  const fullName = user
    ? [user.first_name, user.last_name].filter(Boolean).join(" ")
    : "";
  const username = user?.username ? `@${user.username}` : "";
  let primary = "";

  if (fullName && username) {
    primary = `${fullName} (${username})`;
  } else {
    primary = fullName || username;
  }

  return [
    primary,
    telegramUserId ? `Telegram ID ${telegramUserId}` : "",
  ].filter(Boolean).join(" · ") || "Исполнитель не указан";
}

function getAuditEntityName(event) {
  return event.entity?.display_name
    || event.contest?.name
    || (
      event.entity_id !== null && event.entity_id !== undefined
        ? `${event.entity_type} #${event.entity_id}`
        : "Связанная сущность не указана"
    );
}

function getAuditContestName(event) {
  return event.contest?.name || "";
}

function getAuditTeamName(event, teamId) {
  if (teamId === null || teamId === undefined) {
    return "не указана";
  }
  const relatedTeam = Array.isArray(event.related_teams)
    ? event.related_teams.find((team) => team.id === teamId)
    : null;
  if (relatedTeam?.name) {
    return relatedTeam.name;
  }
  for (const state of [event.after_state, event.before_state]) {
    for (const key of ["home_team", "away_team"]) {
      if (state?.[key]?.id === teamId && state[key].name) {
        return state[key].name;
      }
    }
  }
  return `Команда #${teamId}`;
}

function formatAuditSwissStageResult(value, event) {
  const directTeamIds = Array.isArray(value?.direct_team_ids)
    ? value.direct_team_ids
    : [];
  const eliminationTeamIds = Array.isArray(value?.elimination_team_ids)
    ? value.elimination_team_ids
    : [];
  const formatTeams = (teamIds) => (
    teamIds.map((teamId) => getAuditTeamName(event, teamId)).join(", ") || "—"
  );
  return (
    `Прошли напрямую: ${formatTeams(directTeamIds)}; `
    + `через элиминейшн-раунд: ${formatTeams(eliminationTeamIds)}`
  );
}

function formatAuditScore(state) {
  if (
    !state
    || !Number.isInteger(state.home_score)
    || !Number.isInteger(state.away_score)
  ) {
    return "не указан";
  }
  return `${state.home_score}:${state.away_score}`;
}

function getAuditMatchStatusLabel(status) {
  const labels = {
    scheduled: "запланирован",
    started: "начался",
    finished: "завершён",
    cancelled: "отменён",
  };
  return labels[status] || status || "не указан";
}

function formatAuditBoolean(value, enabledLabel = "включено", disabledLabel = "выключено") {
  if (value === true) {
    return enabledLabel;
  }
  if (value === false) {
    return disabledLabel;
  }
  return "не задано";
}

function addAuditTransition(
  lines,
  label,
  beforeValue,
  afterValue,
  formatter = (value) => String(value),
) {
  if (beforeValue === afterValue) {
    return;
  }
  lines.push(
    `${label}: ${formatter(beforeValue)} → ${formatter(afterValue)}`,
  );
}

function buildContestAuditChanges(event) {
  const before = event.before_state || {};
  const after = event.after_state || {};
  const lines = [];

  addAuditTransition(
    lines,
    "Публикация прогнозов",
    before.match_prediction_publication_enabled,
    after.match_prediction_publication_enabled,
    (value) => formatAuditBoolean(value, "включена", "выключена"),
  );
  addAuditTransition(
    lines,
    "Прогноз на чемпиона",
    before.champion_prediction_enabled,
    after.champion_prediction_enabled,
    (value) => formatAuditBoolean(value, "включён", "выключен"),
  );
  addAuditTransition(
    lines,
    "Дедлайн прогноза на чемпиона",
    before.champion_prediction_deadline_at,
    after.champion_prediction_deadline_at,
    (value) => value ? formatAuditDateTime(value) : "не задан",
  );
  addAuditTransition(
    lines,
    "Баллы за чемпиона",
    before.champion_prediction_points,
    after.champion_prediction_points,
    (value) => value === null || value === undefined ? "не заданы" : String(value),
  );

  return lines;
}

function buildMatchResultAuditChanges(event) {
  const before = event.before_state || {};
  const after = event.after_state || {};
  const lines = [];
  const beforeScore = formatAuditScore(before);
  const afterScore = formatAuditScore(after);

  if (beforeScore !== afterScore) {
    lines.push(`Результат: ${beforeScore} → ${afterScore}`);
  }
  if (before.advancing_team_id !== after.advancing_team_id) {
    const afterTeam = getAuditTeamName(event, after.advancing_team_id);
    if (before.advancing_team_id === null || before.advancing_team_id === undefined) {
      lines.push(`Прошла дальше: ${afterTeam}`);
    } else {
      lines.push(
        "Прошла дальше: "
        + `${getAuditTeamName(event, before.advancing_team_id)} → ${afterTeam}`,
      );
    }
  }
  if (before.status !== after.status) {
    lines.push(
      "Статус: "
      + `${getAuditMatchStatusLabel(before.status)} → `
      + getAuditMatchStatusLabel(after.status),
    );
  }
  return lines;
}

function buildAuditSummaryLines(event) {
  const entityName = getAuditEntityName(event);
  const before = event.before_state || {};
  const after = event.after_state || {};

  switch (event.event_type) {
    case "contest_created":
      return [`Создан конкурс «${entityName}».`];
    case "contest_updated":
      return buildContestAuditChanges(event);
    case "contest_finished":
      return [`Конкурс «${entityName}» завершён.`];
    case "contest_deleted":
      return [`Удалён конкурс «${entityName}».`];
    case "match_created":
      return [`Создан матч «${entityName}».`];
    case "match_updated":
      return [
        "Время начала: "
        + `${formatMatchStartsAt(before.starts_at_utc)} → `
        + `${formatMatchStartsAt(after.starts_at_utc)}.`,
      ];
    case "match_deleted":
      return [`Удалён матч «${entityName}».`];
    case "match_result_set":
    case "match_result_changed":
      return buildMatchResultAuditChanges(event);
    case "contest_champion_set":
      return [
        `Указан чемпион: ${getAuditTeamName(event, after.champion_team_id)}.`,
      ];
    case "contest_champion_changed":
      return [
        "Чемпион изменён: "
        + `${getAuditTeamName(event, before.champion_team_id)} → `
        + `${getAuditTeamName(event, after.champion_team_id)}.`,
      ];
    case "swiss_stage_settings_updated":
      return ["Обновлены настройки прогноза на швейцарский этап."];
    case "swiss_stage_result_set":
      return [
        "Внесены фактические итоги швейцарского этапа.",
        formatAuditSwissStageResult(after.actual_result, event),
      ];
    case "swiss_stage_result_changed":
      return [
        "Исправлены фактические итоги швейцарского этапа.",
        formatAuditSwissStageResult(after.actual_result, event),
      ];
    case "supermoderator_assigned":
      return [`Пользователю ${entityName} назначена роль супермодератора.`];
    case "supermoderator_revoked":
      return [`У пользователя ${entityName} отозвана роль супермодератора.`];
    default:
      return ["Сохранено административное действие."];
  }
}

function formatAuditStateValue(key, value, event) {
  if (value === null || value === undefined || value === "") {
    return "не задано";
  }
  if (key === "status") {
    return getAuditMatchStatusLabel(value);
  }
  if (key === "champion_team_id" || key === "advancing_team_id") {
    return getAuditTeamName(event, value);
  }
  if (
    key === "actual_result"
    && event.entity_type === "swiss_stage_prediction"
    && typeof value === "object"
  ) {
    return formatAuditSwissStageResult(value, event);
  }
  if (key === "is_active") {
    return formatAuditBoolean(value, "активен", "завершён");
  }
  if (key === "champion_prediction_enabled") {
    return formatAuditBoolean(value, "включён", "выключен");
  }
  if (key === "enabled" && event.entity_type === "swiss_stage_prediction") {
    return formatAuditBoolean(value, "включён", "выключен");
  }
  if (key === "match_prediction_publication_enabled") {
    return formatAuditBoolean(value, "включена", "выключена");
  }
  if (
    key.endsWith("_at")
    || key.endsWith("_utc")
  ) {
    return formatAuditDateTime(value);
  }
  if (
    (key === "home_team" || key === "away_team")
    && typeof value === "object"
  ) {
    return value.name || (
      value.id !== undefined ? `Команда #${value.id}` : "не указана"
    );
  }
  if (typeof value === "boolean") {
    return value ? "да" : "нет";
  }
  if (typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

function getAuditStateFieldLabel(key) {
  const labels = {
    id: "ID",
    name: "Название",
    slug: "Короткий адрес",
    is_active: "Статус конкурса",
    created_at: "Создан",
    champion_prediction_enabled: "Прогноз на чемпиона",
    champion_prediction_deadline_at: "Дедлайн прогноза на чемпиона",
    champion_prediction_points: "Баллы за чемпиона",
    champion_team_id: "Чемпион",
    match_prediction_publication_enabled: "Публикация прогнозов",
    match_prediction_publication_enabled_at: "Публикация включена",
    home_team: "Хозяева",
    away_team: "Гости",
    home_score: "Счёт хозяев",
    away_score: "Счёт гостей",
    advancing_team_id: "Прошла дальше",
    starts_at_utc: "Начало матча",
    status: "Статус матча",
    tie_id: "ID противостояния",
    assignment_id: "ID назначения",
    target_telegram_user_id: "Пользователь",
    assigned_at: "Назначен",
    assigned_by_user_id: "Назначил, локальный ID",
    revoked_at: "Отозван",
    revoked_by_user_id: "Отозвал, локальный ID",
    chat_id: "Локальный ID чата",
    user_id: "Локальный ID пользователя",
    enabled: "Прогноз включён",
    deadline_at: "Дедлайн",
    direct_qualifier_count: "Пройдут напрямую",
    elimination_qualifier_count: "Через элиминейшн-раунд",
    teams: "Команды этапа",
    actual_result: "Фактический результат",
  };
  return labels[key] || key.replaceAll("_", " ");
}

function getAuditStateEntries(event, state) {
  if (!state || typeof state !== "object") {
    return [];
  }
  const preferredOrder = event.entity_type === "contest"
    ? [
      "id",
      "name",
      "is_active",
      "match_prediction_publication_enabled",
      "match_prediction_publication_enabled_at",
      "champion_prediction_enabled",
      "champion_prediction_deadline_at",
      "champion_prediction_points",
      "champion_team_id",
      "created_at",
      "slug",
    ]
    : event.entity_type === "match"
      ? [
        "id",
        "home_team",
        "away_team",
        "starts_at_utc",
        "status",
        "home_score",
        "away_score",
        "advancing_team_id",
        "tie_id",
      ]
      : [
        "assignment_id",
        "target_telegram_user_id",
        "assigned_at",
        "revoked_at",
        "assigned_by_user_id",
        "revoked_by_user_id",
        "chat_id",
        "user_id",
      ];
  const keys = [
    ...preferredOrder.filter((key) => Object.hasOwn(state, key)),
    ...Object.keys(state).filter((key) => !preferredOrder.includes(key)).sort(),
  ];
  return keys.map((key) => ({
    label: getAuditStateFieldLabel(key),
    value: key === "target_telegram_user_id"
      ? formatAuditUserIdentity(event.entity?.target_user, state[key])
      : formatAuditStateValue(key, state[key], event),
  }));
}

function createAuditStatePanel(title, event, state, emptyMessage) {
  const panel = createElement("section", {
    className: "audit-state-panel",
  });
  panel.append(createElement("h4", { text: title }));
  const entries = getAuditStateEntries(event, state);
  if (entries.length === 0) {
    panel.append(
      createElement("p", {
        className: "audit-state-empty",
        text: emptyMessage,
      }),
    );
    return panel;
  }
  const list = createElement("dl", {
    className: "audit-state-list",
  });
  for (const entry of entries) {
    list.append(
      createElement("dt", { text: entry.label }),
      createElement("dd", { text: entry.value }),
    );
  }
  panel.append(list);
  return panel;
}

function createAuditEventCard(event) {
  const item = createElement("li", {
    className: "audit-event-item",
  });
  const article = createElement("article", {
    className: "audit-event-card",
  });
  const heading = createElement("h3", {
    text: getAuditEventTitle(event),
  });
  const time = createElement("time", {
    className: "audit-event-time",
    text: formatAuditDateTime(event.created_at),
  });
  time.dateTime = event.created_at;
  const actor = createElement("p", {
    className: "audit-event-meta",
    text: (
      "Исполнитель: "
      + formatAuditUserIdentity(event.actor, event.actor_user_id)
    ),
  });
  const role = createElement("p", {
    className: "audit-event-meta",
    text: `Роль: ${getAuditRoleLabel(event.actor_role)}`,
  });
  const entityParts = [`Связано: ${getAuditEntityName(event)}`];
  const contestName = getAuditContestName(event);
  if (contestName && event.entity_type !== "contest") {
    entityParts.push(`Конкурс: ${contestName}`);
  }
  if (event.contest?.is_deleted) {
    entityParts.push("конкурс удалён");
  }
  const entity = createElement("p", {
    className: "audit-event-entity",
    text: entityParts.join(" · "),
  });
  const summaryLines = buildAuditSummaryLines(event);
  const summary = createElement("p", {
    className: "audit-event-summary",
    text: summaryLines.length > 0
      ? summaryLines.join("\n")
      : "Изменения сохранены.",
  });
  const details = createElement("details", {
    className: "audit-event-details",
  });
  const detailsSummary = createElement("summary", {
    text: "Показать подробности",
  });
  const states = createElement("div", {
    className: "audit-state-grid",
  });
  states.append(
    createAuditStatePanel(
      "До",
      event,
      event.before_state,
      "Сущность не существовала.",
    ),
    createAuditStatePanel(
      "После",
      event,
      event.after_state,
      "Сущность удалена.",
    ),
  );
  details.append(detailsSummary, states);
  article.append(heading, time, actor, role, entity, summary, details);
  item.append(article);
  return item;
}

function createAuditFilterSelect(name, labelText) {
  const field = createElement("label", {
    className: "form-field audit-filter-field",
  });
  const label = createElement("span", {
    className: "form-field-label",
    text: labelText,
  });
  const select = createElement("select", {
    className: "text-input audit-filter-select",
  });
  select.name = name;
  field.append(label, select);
  return { field, select };
}

function appendAuditOption(select, value, label) {
  const option = createElement("option", { text: label });
  option.value = value;
  select.append(option);
}

function createAuditFiltersCard(bootstrap, state) {
  const card = createElement("section", {
    className: "info-card audit-filters-card",
  });
  const heading = createElement("h2", { text: "Фильтры" });
  const fields = createElement("div", {
    className: "audit-filter-grid",
  });
  const contestFilter = createAuditFilterSelect("contest", "Конкурс");
  const eventFilter = createAuditFilterSelect("event-type", "Тип действия");
  const actorFilter = createAuditFilterSelect("actor", "Исполнитель");

  appendAuditOption(contestFilter.select, "", "Все конкурсы");
  appendAuditOption(contestFilter.select, "none", "Без конкурса");
  for (const contest of state.filterOptions.contests) {
    appendAuditOption(
      contestFilter.select,
      String(contest.id),
      `${contest.name}${contest.is_deleted ? " · удалён" : ""}`,
    );
  }
  contestFilter.select.value = state.filters.contestId;

  appendAuditOption(eventFilter.select, "", "Все действия");
  const groups = new Map();
  for (const [eventType, presentation] of Object.entries(
    AUDIT_EVENT_PRESENTATIONS,
  )) {
    if (!groups.has(presentation.group)) {
      groups.set(presentation.group, []);
    }
    groups.get(presentation.group).push({
      eventType,
      label: presentation.label,
    });
  }
  for (const [groupLabel, options] of groups) {
    const group = document.createElement("optgroup");
    group.label = groupLabel;
    for (const optionData of options) {
      const option = createElement("option", { text: optionData.label });
      option.value = optionData.eventType;
      group.append(option);
    }
    eventFilter.select.append(group);
  }
  eventFilter.select.value = state.filters.eventType;

  appendAuditOption(actorFilter.select, "", "Все исполнители");
  for (const actor of state.filterOptions.actors) {
    appendAuditOption(
      actorFilter.select,
      String(actor.telegram_user_id),
      formatAuditUserIdentity(actor, actor.telegram_user_id),
    );
  }
  actorFilter.select.value = state.filters.actorUserId;

  const applyFilters = () => {
    state.filters = {
      contestId: contestFilter.select.value,
      eventType: eventFilter.select.value,
      actorUserId: actorFilter.select.value,
    };
    void loadAuditEvents(bootstrap, state, false);
  };
  for (const select of [
    contestFilter.select,
    eventFilter.select,
    actorFilter.select,
  ]) {
    select.disabled = state.loading;
    select.addEventListener("change", applyFilters);
  }

  fields.append(
    contestFilter.field,
    eventFilter.field,
    actorFilter.field,
  );
  card.append(heading, fields);
  return card;
}

function hasActiveAuditFilters(state) {
  return Boolean(
    state.filters.contestId
    || state.filters.eventType
    || state.filters.actorUserId,
  );
}

function getAuditErrorMessage(error) {
  if (error?.code === "audit_cursor_invalid") {
    return "Не удалось продолжить загрузку истории. Обновите список.";
  }
  if (error?.code === "audit_data_invalid") {
    return (
      "Одна из записей истории повреждена. "
      + "Попробуйте ещё раз позже."
    );
  }
  if (error?.code === "contest_management_forbidden") {
    return "История действий доступна администраторам и супермодераторам.";
  }
  return "Не удалось загрузить историю действий. Попробуйте ещё раз.";
}

function createAuditListCard(bootstrap, state) {
  const card = createElement("section", {
    className: "info-card audit-list-card",
  });
  card.append(createElement("h2", { text: "События" }));

  if (state.loading && !state.initialized) {
    card.append(
      createStatusCard(
        "Загружаем историю",
        "Получаем последние административные действия этого чата…",
      ),
    );
    return card;
  }

  if (state.error && state.events.length === 0) {
    const message = createElement("p", {
      className: "form-message is-error",
      text: state.error,
    });
    const retryButton = createActionButton(
      "Попробовать снова",
      "secondary-action-button",
    );
    retryButton.addEventListener("click", () => {
      void loadAuditEvents(bootstrap, state, false);
    });
    card.append(message, retryButton);
    return card;
  }

  if (state.events.length === 0) {
    card.append(
      createElement("p", {
        className: "subtitle",
        text: hasActiveAuditFilters(state)
          ? "По выбранным фильтрам действий нет."
          : "История действий пока пуста.",
      }),
    );
    return card;
  }

  const list = createElement("ol", {
    className: "audit-event-list",
  });
  for (const event of state.events) {
    list.append(createAuditEventCard(event));
  }
  card.append(list);

  if (state.error) {
    const errorPanel = createElement("div", {
      className: "audit-load-more-error",
    });
    const message = createElement("p", {
      className: "form-message is-error",
      text: state.error,
    });
    const retryButton = createActionButton(
      "Повторить загрузку",
      "secondary-action-button",
    );
    retryButton.addEventListener("click", () => {
      void loadAuditEvents(bootstrap, state, true);
    });
    errorPanel.append(message, retryButton);
    card.append(errorPanel);
  } else if (state.nextCursor) {
    const loadMoreButton = createActionButton(
      state.loading ? "Загружаем…" : "Показать ещё",
      "secondary-action-button audit-load-more-button",
    );
    loadMoreButton.disabled = state.loading;
    loadMoreButton.addEventListener("click", () => {
      void loadAuditEvents(bootstrap, state, true);
    });
    card.append(loadMoreButton);
  }

  return card;
}

function createAuditHeaderCard(bootstrap) {
  return createAdministrativeHeader(bootstrap, {
    title: "Журнал действий",
    backLabel: "← К управлению",
    description: (
      "Административные действия этого Telegram-чата — "
      + "от новых к старым."
    ),
    className: "audit-header-card",
    onBack: () => {
      void openManagement(bootstrap);
    },
  });
}

function renderAuditScreen(bootstrap, state) {
  currentViewMode = "management";
  setChatSummary();
  appContentElement.replaceChildren(
    createAuditHeaderCard(bootstrap),
    createAuditFiltersCard(bootstrap, state),
    createAuditListCard(bootstrap, state),
  );
}

function buildAuditRequestPath(state, append) {
  const parameters = new URLSearchParams();
  parameters.set("limit", String(AUDIT_PAGE_SIZE));
  if (state.filters.contestId === "none") {
    parameters.set("entity_type", "supermoderator_assignment");
  } else if (state.filters.contestId) {
    parameters.set("contest_id", state.filters.contestId);
  }
  if (state.filters.eventType) {
    parameters.set("event_type", state.filters.eventType);
  }
  if (state.filters.actorUserId) {
    parameters.set("actor_user_id", state.filters.actorUserId);
  }
  if (append && state.nextCursor) {
    parameters.set("cursor", state.nextCursor);
  }
  return `/api/tma/audit-events?${parameters.toString()}`;
}

async function loadAuditEvents(bootstrap, state, append) {
  if (state.loading) {
    return;
  }
  state.loading = true;
  state.error = "";
  if (!append) {
    state.events = [];
    state.nextCursor = null;
    state.initialized = false;
  }
  renderAuditScreen(bootstrap, state);

  let managementRequestFailed = false;
  try {
    const result = await apiRequest(buildAuditRequestPath(state, append));
    const incomingEvents = Array.isArray(result.events) ? result.events : [];
    const combined = append ? [...state.events, ...incomingEvents] : incomingEvents;
    const seenEventIds = new Set();
    state.events = combined.filter((event) => {
      if (seenEventIds.has(event.id)) {
        return false;
      }
      seenEventIds.add(event.id);
      return true;
    });
    state.nextCursor = result.next_cursor || null;
    const filterOptions = result.filter_options || {};
    state.filterOptions = {
      contests: Array.isArray(filterOptions.contests)
        ? filterOptions.contests
        : state.filterOptions.contests,
      actors: Array.isArray(filterOptions.actors)
        ? filterOptions.actors
        : state.filterOptions.actors,
    };
    state.initialized = true;
  } catch (error) {
    if (handleManagementRequestError(error)) {
      managementRequestFailed = true;
      return;
    }
    state.error = getAuditErrorMessage(error);
    state.initialized = true;
  } finally {
    state.loading = false;
    if (!managementRequestFailed) {
      renderAuditScreen(bootstrap, state);
    }
  }
}

function openAuditHistory(bootstrap) {
  const state = {
    events: [],
    nextCursor: null,
    filters: {
      contestId: "",
      eventType: "",
      actorUserId: "",
    },
    filterOptions: {
      contests: [],
      actors: [],
    },
    loading: false,
    initialized: false,
    error: "",
  };
  void loadAuditEvents(bootstrap, state, false);
}

function createSupermoderatorManagementCard() {
  const card = createElement("section", {
    className: "info-card role-management-card",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: (
      "Супермодератор назначается для всего Telegram-чата и сохраняет роль "
      + "до явного отзыва."
    ),
  });
  const warning = createElement("p", {
    className: "role-management-warning",
    text: (
      "Членство пользователя в Telegram-чате не проверяется. "
      + "Проверьте, что назначаете нужного человека."
    ),
  });
  const listHeading = createElement("h3", { text: "Активные назначения" });
  const listContainer = createElement("div", {
    className: "supermoderator-list",
  });
  const listStatus = createElement("p", {
    className: "form-message",
    text: "Загружаем назначения…",
  });
  const formHeading = createElement("h3", { text: "Добавить супермодератора" });
  const form = createElement("form", { className: "form-fields" });
  const field = createElement("label", { className: "form-field" });
  const fieldLabel = createElement("span", {
    className: "form-field-label",
    text: "Telegram ID или точный username",
  });
  const input = createElement("input", { className: "text-input" });
  const findButton = createActionButton(
    "Найти",
    "secondary-action-button",
    "submit",
  );
  const formMessage = createElement("p", { className: "form-message" });
  const preview = createElement("div", {
    className: "supermoderator-preview",
  });
  let resolvedUser = null;
  let activeOperation = null;

  input.type = "text";
  input.name = "telegram-user";
  input.placeholder = "123456789 или @username";
  input.autocomplete = "off";
  input.required = true;

  const fieldHint = createElement("p", {
    className: "form-hint",
    text: "Допустимые форматы: положительный Telegram ID или точный @username.",
  });

  field.append(fieldLabel, input);
  form.append(field, fieldHint, findButton, formMessage, preview);
  listContainer.append(listStatus);
  card.append(
    description,
    warning,
    listHeading,
    listContainer,
    formHeading,
    form,
  );

  const loadAssignments = async () => {
    setFormMessage(listStatus, "Загружаем назначения…");
    listContainer.replaceChildren(listStatus);
    try {
      const result = await apiRequest("/api/tma/access/supermoderators");
      renderSupermoderatorAssignments(
        listContainer,
        Array.isArray(result.assignments) ? result.assignments : [],
        loadAssignments,
      );
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        listStatus,
        getRoleManagementErrorMessage(error),
        "error",
      );
      listContainer.replaceChildren(listStatus);
    }
  };

  input.addEventListener("input", () => {
    resolvedUser = null;
    preview.replaceChildren();
    setFormMessage(formMessage, "");
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (activeOperation !== null) {
      return;
    }
    const target = input.value;
    if (!target) {
      setFormMessage(
        formMessage,
        "Введите Telegram ID или точный username.",
        "error",
      );
      return;
    }
    activeOperation = "resolve";
    resolvedUser = null;
    preview.replaceChildren();
    input.disabled = true;
    findButton.disabled = true;
    findButton.textContent = "Ищем…";
    setFormMessage(formMessage, "Ищем пользователя в Telegram…");
    try {
      const result = await apiRequest("/api/tma/access/users/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target }),
      });
      resolvedUser = result.user;
      setFormMessage(formMessage, "");
      renderResolvedSupermoderator(
        preview,
        result,
        async (selectedUser) => {
          activeOperation = "assign";
          resolvedUser = null;
          input.disabled = true;
          findButton.disabled = true;
          findButton.textContent = "Назначаем…";
          try {
            await apiRequest(
              `/api/tma/access/supermoderators/${selectedUser.telegram_user_id}`,
              { method: "PUT" },
            );
            setFormMessage(
              formMessage,
              `${getRoleTargetDisplayName(selectedUser)} назначен супермодератором.`,
              "success",
            );
            preview.replaceChildren();
            await loadAssignments();
          } catch (error) {
            if (handleManagementRequestError(error)) {
              return;
            }
            throw error;
          } finally {
            activeOperation = null;
            input.disabled = false;
            findButton.disabled = false;
            findButton.textContent = "Найти";
          }
        },
      );
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        formMessage,
        getRoleManagementErrorMessage(error),
        "error",
      );
    } finally {
      if (activeOperation === "resolve") {
        activeOperation = null;
      }
      input.disabled = false;
      findButton.disabled = false;
      findButton.textContent = "Найти";
    }
  });

  void loadAssignments();
  return card;
}

function renderSupermoderatorManagementScreen(bootstrap) {
  currentViewMode = "management";
  setChatSummary();
  appContentElement.replaceChildren(
    createAdministrativeHeader(bootstrap, {
      title: "Супермодераторы",
      backLabel: "← К управлению",
      onBack: () => {
        void openManagement(bootstrap);
      },
    }),
    createSupermoderatorManagementCard(),
  );
}

function openSupermoderatorManagement(bootstrap) {
  renderSupermoderatorManagementScreen(bootstrap);
}

function renderSupermoderatorAssignments(container, assignments, reload) {
  if (assignments.length === 0) {
    container.replaceChildren(
      createElement("p", {
        className: "subtitle",
        text: "Активных назначений пока нет.",
      }),
    );
    return;
  }
  const list = createElement("ul", { className: "supermoderator-items" });
  for (const assignment of assignments) {
    const item = createElement("li", { className: "supermoderator-item" });
    const title = createElement("strong", {
      text: getRoleTargetDisplayName(assignment.user),
    });
    const identityParts = [];
    if (assignment.user.username) {
      identityParts.push(`@${assignment.user.username}`);
    }
    identityParts.push(`Telegram ID ${assignment.user.telegram_user_id}`);
    const role = assignment.is_telegram_admin
      ? "Администратор Telegram · локальное назначение активно"
      : "Супермодератор";
    const details = createElement("p", {
      className: "supermoderator-meta",
      text: identityParts.join(" · "),
    });
    const assignmentDetails = createElement("p", {
      className: "supermoderator-meta",
      text: (
        `${role}. Назначен ${formatRoleAssignmentDate(assignment.assigned_at)} `
        + `пользователем ${getUserDisplayName(assignment.assigned_by)}.`
      ),
    });
    const revokeButton = createActionButton(
      "Отозвать",
      "danger-action-button",
    );
    const message = createElement("p", { className: "form-message" });
    revokeButton.addEventListener("click", async () => {
      if (!window.confirm(`Отозвать роль у ${getRoleTargetDisplayName(assignment.user)}?`)) {
        return;
      }
      revokeButton.disabled = true;
      revokeButton.textContent = "Отзываем…";
      try {
        await apiRequest(
          `/api/tma/access/supermoderators/${assignment.user.telegram_user_id}`,
          { method: "DELETE" },
        );
        await reload();
      } catch (error) {
        if (handleManagementRequestError(error)) {
          return;
        }
        setFormMessage(message, getRoleManagementErrorMessage(error), "error");
        revokeButton.disabled = false;
        revokeButton.textContent = "Отозвать";
      }
    });
    item.append(title, details, assignmentDetails, revokeButton, message);
    list.append(item);
  }
  container.replaceChildren(list);
}

function renderResolvedSupermoderator(container, result, assign) {
  const selectedUser = Object.freeze({ ...result.user });
  const panel = createElement("div", { className: "confirmation-panel" });
  const title = createElement("strong", {
    text: getRoleTargetDisplayName(selectedUser),
  });
  const identityParts = [];
  if (selectedUser.username) {
    identityParts.push(`@${selectedUser.username}`);
  }
  identityParts.push(`Telegram ID ${selectedUser.telegram_user_id}`);
  const details = createElement("p", {
    className: "supermoderator-meta",
    text: identityParts.join(" · "),
  });
  const message = createElement("p", { className: "form-message" });
  const assignButton = createActionButton(
    result.has_active_assignment ? "Уже назначен" : "Назначить супермодератором",
    "primary-action-button",
  );
  assignButton.disabled = result.has_active_assignment;
  assignButton.addEventListener("click", async () => {
    assignButton.disabled = true;
    assignButton.textContent = "Назначаем…";
    try {
      await assign(selectedUser);
    } catch (error) {
      setFormMessage(message, getRoleManagementErrorMessage(error), "error");
      assignButton.disabled = false;
      assignButton.textContent = "Назначить супермодератором";
    }
  });
  panel.append(title, details, assignButton, message);
  container.replaceChildren(panel);
}

function createRoleManagementUnavailableCard() {
  return createInfoCard(
    "Управление супермодераторами временно недоступно",
    [
      "Не удалось проверить права администратора Telegram.",
      "Поэтому управление супермодераторами временно недоступно. "
        + "Доступ к управлению конкурсами определяется отдельно. "
        + "Просмотр и прогнозирование продолжают работать.",
    ],
    "role-management-unavailable-card",
  );
}

function canManageContests(bootstrap) {
  return bootstrap.can_access_management === true;
}

function isContestManagementVerificationUnavailable(bootstrap) {
  return (
    bootstrap.access?.enforcement_enabled === true
    && bootstrap.access?.can_manage_contests !== true
    && bootstrap.access?.verification_status === "unavailable"
  );
}

function createContestManagementUnavailableCard() {
  return createInfoCard(
    "Управление конкурсами временно недоступно",
    [
      "Не удалось проверить права администратора Telegram. "
        + "Управление конкурсами временно недоступно. "
        + "Просмотр и прогнозирование продолжают работать.",
    ],
    "contest-management-unavailable-card",
  );
}

function createContestManagementRestrictedCard() {
  return createInfoCard(
    "Управление конкурсами",
    [
      "Создавать и настраивать конкурсы могут администраторы чата "
        + "и супермодераторы.",
    ],
    "contest-management-restricted-card",
  );
}

function getRoleTargetDisplayName(user) {
  const fullName = [user.first_name, user.last_name]
    .filter(Boolean)
    .join(" ");
  return fullName || `Telegram ID ${user.telegram_user_id}`;
}

function getRoleManagementErrorMessage(error) {
  const code = error && typeof error === "object" ? error.code : "";
  if (code === "username_not_found") {
    return (
      "Пользователь с таким username не найден. "
      + "Проверьте написание и попробуйте ещё раз."
    );
  }
  if (code === "telegram_user_id_invalid") {
    return "Telegram ID должен быть положительным целым числом.";
  }
  if (code === "username_invalid" || code === "role_target_invalid") {
    return "Укажите положительный Telegram ID или точный @username.";
  }
  if (code === "username_target_not_supported") {
    return "Супермодератором можно назначить только обычного пользователя Telegram.";
  }
  if (code === "username_resolution_not_configured") {
    return (
      "Поиск по username сейчас не настроен. "
      + "Можно назначить пользователя по Telegram ID."
    );
  }
  if (code === "username_resolution_unavailable" || code === "telegram_flood_wait") {
    return "Не удалось найти пользователя в Telegram. Попробуйте ещё раз позже.";
  }
  if (code === "telegram_admin_verification_unavailable") {
    return (
      "Не удалось подтвердить права администратора Telegram. "
      + "Попробуйте ещё раз позже."
    );
  }
  return error instanceof Error ? error.message : "Не удалось выполнить операцию.";
}

function createManagementNavigationCard(bootstrap) {
  const card = createElement("section", {
    className: "info-card management-navigation-card",
  });
  const heading = createElement("h2", { text: "Управление" });
  const description = createElement("p", {
    className: "subtitle",
    text: "Матчи, результаты, настройки конкурса и доступы вынесены в отдельный раздел.",
  });
  const button = createActionButton(
    "Открыть управление",
    "secondary-action-button",
  );
  button.addEventListener("click", () => {
    void openManagement(bootstrap);
  });
  card.append(heading, description, button);
  return card;
}

function createAdministrativeHeader(
  bootstrap,
  {
    title,
    backLabel,
    onBack,
    description = "",
    className = "",
  },
) {
  const chatTitle = bootstrap.context?.chat?.title || "этого чата";
  const card = createElement("section", {
    className: [
      "contest-overview",
      "management-overview",
      "administrative-header",
      className,
    ]
      .filter(Boolean)
      .join(" "),
  });
  const backButton = createActionButton(backLabel, "contest-back-link");
  const heading = createElement("h2", { text: title });
  const context = createElement("p", {
    className: "subtitle management-context",
    text: `Чат «${chatTitle}»`,
  });

  backButton.addEventListener("click", onBack);
  card.append(backButton, heading, context);

  if (description) {
    card.append(
      createElement("p", {
        className: "subtitle administrative-header-description",
        text: description,
      }),
    );
  }

  return card;
}

function createManagementContestRow(contest, bootstrap) {
  const item = createElement("li", {
    className: "management-list-item",
  });
  const button = createActionButton(
    "",
    "secondary-action-button management-navigation-row management-contest-button",
  );
  const copy = createElement("span", {
    className: "management-row-copy",
  });
  const title = createElement("span", {
    className: "management-row-title",
    text: contest.name,
  });
  const statusLabel = contest.status === "completed" ? "Завершён" : "Активен";
  const status = createElement("span", {
    className: "management-contest-meta",
    text: statusLabel,
  });
  const chevron = createElement("span", {
    className: "management-row-chevron",
    text: "›",
  });

  button.setAttribute("aria-label", `${contest.name}. ${statusLabel}`);
  chevron.setAttribute("aria-hidden", "true");
  copy.append(title, status);
  button.append(copy, chevron);
  button.addEventListener("click", () => {
    void openContest(bootstrap, contest.id, { managementMode: true });
  });
  item.append(button);
  return item;
}

function createManagementContestGroup(
  title,
  contests,
  bootstrap,
  emptyMessage = "",
) {
  const group = createElement("section", {
    className: "management-contest-group",
  });
  const heading = createElement("h3", { text: title });
  group.append(heading);

  if (contests.length === 0) {
    if (emptyMessage) {
      group.append(
        createElement("p", {
          className: "subtitle management-group-empty-state",
          text: emptyMessage,
        }),
      );
    }
    return group;
  }

  const list = createElement("ul", {
    className: "management-navigation-list",
  });
  for (const contest of contests) {
    list.append(createManagementContestRow(contest, bootstrap));
  }
  group.append(list);
  return group;
}

function createCompletedManagementContestGroup(contests, bootstrap) {
  const disclosure = createElement("details", {
    className: "management-contest-group management-completed-contests",
  });
  const summary = createElement("summary", {
    text: "Завершённые",
  });
  const list = createElement("ul", {
    className: "management-navigation-list",
  });

  for (const contest of contests) {
    list.append(createManagementContestRow(contest, bootstrap));
  }
  disclosure.append(summary, list);
  return disclosure;
}

function createManagementContestListCard(
  contests,
  bootstrap,
  managementData,
) {
  const capabilities = managementData.capabilities || {};
  const activeContests = contests.filter(
    (contest) => contest.status === "active",
  );
  const completedContests = contests.filter(
    (contest) => contest.status === "completed",
  );
  const card = createElement("section", {
    className: "info-card management-contest-list-card",
  });
  const sectionHeader = createElement("div", {
    className: "management-section-header",
  });
  const heading = createElement("h2", { text: "Конкурсы" });
  sectionHeader.append(heading);

  if (capabilities.can_create_contests === true) {
    const createButton = createActionButton(
      "Создать",
      "secondary-action-button management-create-button",
    );
    createButton.addEventListener("click", () => {
      openContestCreation(bootstrap, managementData);
    });
    sectionHeader.append(createButton);
  }

  card.append(sectionHeader);

  if (contests.length === 0) {
    const emptyState = createElement("div", {
      className: "management-empty-state",
    });
    emptyState.append(
      createElement("p", {
        className: "management-empty-title",
        text: "Конкурсов в этом чате пока нет.",
      }),
      createElement("p", {
        className: "subtitle",
        text: (
          "Создайте первый конкурс, чтобы добавить матчи "
          + "и принимать прогнозы."
        ),
      }),
    );

    if (capabilities.can_create_contests === true) {
      const createContestButton = createActionButton(
        "Создать конкурс",
        "primary-action-button",
      );
      createContestButton.addEventListener("click", () => {
        openContestCreation(bootstrap, managementData);
      });
      emptyState.append(createContestButton);
    }

    card.append(emptyState);
    return card;
  }

  const groups = createElement("div", {
    className: "management-contest-groups",
  });
  groups.append(
    createManagementContestGroup(
      "Активные",
      activeContests,
      bootstrap,
      "Активных конкурсов сейчас нет.",
    ),
  );
  if (completedContests.length > 0) {
    groups.append(
      createCompletedManagementContestGroup(completedContests, bootstrap),
    );
  }
  card.append(groups);
  return card;
}

function createManagementNavigationRow(title, description, onOpen) {
  const button = createActionButton(
    "",
    "secondary-action-button management-navigation-row",
  );
  const copy = createElement("span", {
    className: "management-row-copy",
  });
  const heading = createElement("span", {
    className: "management-row-title",
    text: title,
  });
  const details = createElement("span", {
    className: "management-row-description",
    text: description,
  });
  const chevron = createElement("span", {
    className: "management-row-chevron",
    text: "›",
  });

  button.setAttribute("aria-label", `${title}. ${description}`);
  chevron.setAttribute("aria-hidden", "true");
  copy.append(heading, details);
  button.append(copy, chevron);
  button.addEventListener("click", onOpen);
  return button;
}

function createManagementAccessCard(bootstrap, capabilities) {
  const rows = [];

  if (capabilities.can_read_audit === true) {
    rows.push(
      createManagementNavigationRow(
        "Журнал действий",
        "История административных изменений",
        () => {
          openAuditHistory(bootstrap);
        },
      ),
    );
  }
  if (capabilities.can_manage_roles === true) {
    rows.push(
      createManagementNavigationRow(
        "Супермодераторы",
        "Управление дополнительными правами",
        () => {
          openSupermoderatorManagement(bootstrap);
        },
      ),
    );
  }

  if (rows.length === 0) {
    return null;
  }

  const card = createElement("section", {
    className: "info-card management-access-card",
  });
  const heading = createElement("h2", {
    text: "Доступ и аудит",
  });
  const list = createElement("div", {
    className: "management-navigation-list",
  });
  list.append(...rows);
  card.append(heading, list);
  return card;
}

function createManagementHeaderCard(bootstrap) {
  return createAdministrativeHeader(bootstrap, {
    title: "Управление",
    backLabel: "← К конкурсам",
    onBack: () => {
      void openContestList(bootstrap);
    },
  });
}

function renderManagementScreen(bootstrap, managementData, state = {}) {
  currentViewMode = "management";
  setChatSummary();
  const contests = Array.isArray(managementData.contests)
    ? managementData.contests
    : [];
  const capabilities = managementData.capabilities || {};
  const cards = [
    createManagementHeaderCard(bootstrap),
    createManagementContestListCard(contests, bootstrap, managementData),
  ];

  if (state.accessMessage) {
    cards.splice(
      1,
      0,
      createInfoCard("Доступ изменился", [state.accessMessage]),
    );
  }

  const accessCard = createManagementAccessCard(bootstrap, capabilities);
  if (accessCard) {
    cards.push(accessCard);
  }
  appContentElement.replaceChildren(...cards);
}

function handleManagementRequestError(
  error,
  message = "Права изменились. Недоступный экран управления закрыт.",
) {
  if (error?.status !== 403 || !activeBootstrap) {
    return false;
  }
  void openManagement(activeBootstrap, {
    accessMessage: message,
  });
  return true;
}

async function openManagement(bootstrap, state = {}) {
  activeBootstrap = bootstrap;
  currentViewMode = "management";
  setChatSummary();
  appContentElement.replaceChildren(
    createStatusCard(
      "Открываем управление",
      "Проверяем права и загружаем конкурсы текущего чата…",
    ),
  );
  try {
    const result = await apiRequest("/api/tma/management/contests");
    renderManagementScreen(bootstrap, result || {}, state);
  } catch (error) {
    if (error?.status === 403) {
      const nextBootstrap = {
        ...bootstrap,
        can_access_management: false,
      };
      activeBootstrap = nextBootstrap;
      await openContestList(nextBootstrap, {
        managementMessage: "Доступ к разделу «Управление» отсутствует.",
      });
      return;
    }
    const message = error instanceof Error
      ? error.message
      : "Не удалось открыть управление.";
    appContentElement.replaceChildren(
      createManagementHeaderCard(bootstrap),
      createInfoCard("Не удалось открыть управление", [message]),
    );
  }
}

async function openContestList(bootstrap, state = {}) {
  renderLoading();

  try {
    const refreshedBootstrap = await apiRequest("/api/tma/bootstrap");
    activeBootstrap = refreshedBootstrap;
    renderContestScreen(refreshedBootstrap, state);
  } catch (error) {
    const message = error instanceof Error
      ? error.message
      : "Не удалось обновить список конкурсов.";
    activeBootstrap = bootstrap;
    renderContestScreen(bootstrap, {
      ...state,
      contestListMessage: message,
    });
  }
}

function renderContestScreen(bootstrap, state = {}) {
  currentViewMode = "participant";
  const { user, chat } = bootstrap.context;
  const chatTitle = chat.title || "этого чата";
  const userName = getUserDisplayName(user);
  const activeContests = Array.isArray(bootstrap.active_contests)
    ? bootstrap.active_contests
    : [];
  const completedContests = Array.isArray(bootstrap.completed_contests)
    ? bootstrap.completed_contests
    : [];

  setChatSummary(`Привет, ${userName}. Чат «${chatTitle}».`);

  const contestCard = createContestsCard(
    activeContests,
    completedContests,
    (contestId) => {
      void openContest(bootstrap, contestId);
    },
  );
  const cards = [contestCard];
  if (state.contestListMessage) {
    cards.push(
      createInfoCard(
        "Не удалось обновить список конкурсов",
        [state.contestListMessage],
      ),
    );
  }
  if (state.managementMessage) {
    cards.push(createInfoCard("Управление недоступно", [state.managementMessage]));
  }
  if (canManageContests(bootstrap)) {
    cards.push(createManagementNavigationCard(bootstrap));
  }
  appContentElement.replaceChildren(...cards);
}

function renderBootstrap(bootstrap) {
  activeBootstrap = bootstrap;
  renderContestScreen(bootstrap);
}

function renderError(message) {
  setChatSummary("Не удалось открыть конкурсы.");
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
    const message = detail && typeof detail === "object"
      ? detail.message
      : detail;
    const error = new Error(message || `HTTP ${response.status}`);
    error.status = response.status;
    if (detail && typeof detail === "object") {
      error.code = detail.code || "";
    }
    throw error;
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
