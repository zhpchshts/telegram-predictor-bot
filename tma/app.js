"use strict";

let telegram = window.Telegram?.WebApp || null;
let activeBootstrap = null;
let currentViewToken = 0;

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
const PREDICTION_DEADLINE_SYNC_EVENT = "tma:prediction-deadline-sync";
const PREDICTION_FLUSH_EVENT = "tma:prediction-flush";
const PREDICTION_SAVE_WAIT_TIMEOUT_MS = 15_000;
const MAX_TIMER_DELAY_MS = 2_147_000_000;
const CONTEST_NAME_MAX_LENGTH = 80;
const CONTEST_TABS = [
  { id: "matches", label: "Матчи" },
  { id: "tournament", label: "Турнир" },
  { id: "leaderboard", label: "Рейтинг" },
];
const CONTEST_MANAGEMENT_TABS = [
  { id: "matches", label: "Матчи" },
  { id: "settings", label: "Настройки" },
  { id: "publications", label: "Публикации" },
];
const AUDIT_EVENT_PRESENTATIONS = Object.freeze({
  chat_settings_updated: Object.freeze({
    label: "Изменён текст кнопки",
    group: "Чат",
  }),
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
  tournament_teams_updated: Object.freeze({
    label: "Изменены команды турнира",
    group: "Конкурсы",
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
  intermediate_leaderboard_publication_requested: Object.freeze({
    label: "Запрошена публикация промежуточного рейтинга",
    group: "Конкурсы",
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
const matchPredictionSaveQueues = new Map();

const chatSummaryElement = document.querySelector("#chat-summary");
const appContentElement = document.querySelector("#app-content");

function flushMatchPredictionForms() {
  for (const form of appContentElement.querySelectorAll(
    ".match-prediction-form",
  )) {
    form.dispatchEvent(new Event(PREDICTION_FLUSH_EVENT));
  }
}

function replaceAppContent(...children) {
  flushMatchPredictionForms();
  currentViewToken += 1;
  appContentElement.replaceChildren(...children);
  return currentViewToken;
}

function isCurrentView(viewToken) {
  return viewToken === currentViewToken;
}

class StaleViewRequestError extends Error {
  constructor() {
    super("The view changed before the request completed.");
    this.name = "StaleViewRequestError";
  }
}

async function apiRequestForCurrentView(path, options = {}) {
  const viewToken = currentViewToken;
  let result;
  try {
    result = await apiRequest(path, options);
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      throw new StaleViewRequestError();
    }
    throw error;
  }
  if (!isCurrentView(viewToken)) {
    throw new StaleViewRequestError();
  }
  return result;
}

function queueMatchPredictionSave(contestId, matchId, payload) {
  const queueKey = `${contestId}:${matchId}`;
  const sendSave = () => apiRequest(
    `/api/tma/contests/${contestId}/matches/${matchId}/prediction`,
    {
      method: "PUT",
      keepalive: true,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  );
  const previousSave = matchPredictionSaveQueues.get(queueKey);
  const request = previousSave
    ? previousSave.catch(() => undefined).then(sendSave)
    : sendSave();
  let trackedRequest;
  trackedRequest = request.finally(() => {
    if (matchPredictionSaveQueues.get(queueKey) === trackedRequest) {
      matchPredictionSaveQueues.delete(queueKey);
    }
  });
  void trackedRequest.catch(() => undefined);
  matchPredictionSaveQueues.set(queueKey, trackedRequest);
  return trackedRequest;
}

async function waitForMatchPredictionSaves(contestId) {
  const queuePrefix = `${contestId}:`;
  const drainSaves = async () => {
    while (true) {
      const pendingSaves = [...matchPredictionSaveQueues.entries()]
        .filter(([queueKey]) => queueKey.startsWith(queuePrefix))
        .map(([, request]) => request);
      if (pendingSaves.length === 0) {
        return;
      }
      await Promise.allSettled(pendingSaves);
    }
  };
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = window.setTimeout(() => {
      reject(new Error(
        "Не удалось дождаться сохранения прогноза. "
        + "Проверьте соединение и откройте конкурс ещё раз.",
      ));
    }, PREDICTION_SAVE_WAIT_TIMEOUT_MS);
  });

  try {
    await Promise.race([drainSaves(), timeout]);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function syncVisiblePredictionDeadlines() {
  for (const form of appContentElement.querySelectorAll(
    ".match-prediction-form[data-prediction-deadline]",
  )) {
    form.dispatchEvent(new Event(PREDICTION_DEADLINE_SYNC_EVENT));
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    flushMatchPredictionForms();
    return;
  }
  if (document.visibilityState === "visible") {
    syncVisiblePredictionDeadlines();
  }
});

window.addEventListener("pagehide", flushMatchPredictionForms);
window.addEventListener("pageshow", syncVisiblePredictionDeadlines);

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

function buildInactiveLaunchTokenMessage() {
  return (
    "Эта кнопка больше не действует.\n" +
    "Отправь /app в обновлённом чате и открой Клевер через новую кнопку."
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
  card.setAttribute("role", "status");
  card.setAttribute("aria-live", "polite");
  card.setAttribute("aria-atomic", "true");

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
  messageElement.setAttribute("role", type === "error" ? "alert" : "status");
  messageElement.setAttribute(
    "aria-live",
    type === "error" ? "assertive" : "polite",
  );
  messageElement.setAttribute("aria-atomic", "true");
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
  return replaceAppContent(
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

function getCreatableContestTemplates(value) {
  if (!Array.isArray(value)) {
    return [];
  }

  const templates = [];
  const seenKeys = new Set();
  for (const template of value) {
    const key = typeof template?.key === "string" ? template.key.trim() : "";
    const label = typeof template?.label === "string"
      ? template.label.trim()
      : "";
    if (!key || !label || seenKeys.has(key)) {
      continue;
    }
    templates.push({ key, label });
    seenKeys.add(key);
  }
  return templates;
}

function fillContestTemplateSelect(select, templates, selectedKey = "") {
  select.replaceChildren();
  if (templates.length === 0) {
    const emptyOption = document.createElement("option");
    emptyOption.value = "";
    emptyOption.textContent = "Шаблонов нет";
    emptyOption.disabled = true;
    emptyOption.selected = true;
    select.append(emptyOption);
    select.disabled = true;
    return;
  }

  for (const template of templates) {
    const option = document.createElement("option");
    option.value = template.key;
    option.textContent = template.label;
    select.append(option);
  }
  select.value = templates.some((template) => template.key === selectedKey)
    ? selectedKey
    : templates[0].key;
  select.disabled = false;
}

function createContestFormCard(bootstrap, state) {
  const contestTemplates = getCreatableContestTemplates(
    state.managementData?.contest_templates,
  );
  const creatableTemplateKeys = new Set(
    contestTemplates.map((template) => template.key),
  );
  const hasTemplates = contestTemplates.length > 0;
  const card = createElement("section", {
    className: "info-card contest-form-card",
  });
  const heading = createElement("h2", {
    text: "Создать конкурс",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Выберите шаблон и настройте новый конкурс прогнозов.",
  });
  const form = createElement("form", {
    className: "form-fields",
  });
  const field = createElement("label", {
    className: "form-field",
  });
  const templateField = createElement("label", {
    className: "form-field",
  });
  const templateLabel = createElement("span", {
    className: "form-field-label",
    text: "Шаблон конкурса",
  });
  const templateInput = document.createElement("select");
  templateInput.className = "text-input";
  templateInput.id = "contest-template-key";
  templateInput.name = "contest-template-key";
  fillContestTemplateSelect(
    templateInput,
    contestTemplates,
    state.draftTemplateKey,
  );
  const sharedTournaments = (
    Array.isArray(state.managementData?.shared_tournaments)
      ? state.managementData.shared_tournaments
      : []
  ).filter((tournament) => creatableTemplateKeys.has(tournament.template_key));
  const sourceField = createElement("label", {
    className: "form-field",
  });
  const sourceLabel = createElement("span", {
    className: "form-field-label",
    text: "Расписание",
  });
  const sourceInput = document.createElement("select");
  sourceInput.className = "text-input";
  sourceInput.id = "contest-shared-tournament";
  sourceInput.name = "contest-shared-tournament";
  const independentOption = document.createElement("option");
  independentOption.value = "";
  independentOption.textContent = "Независимый конкурс";
  sourceInput.append(independentOption);
  for (const tournament of sharedTournaments) {
    const option = document.createElement("option");
    option.value = String(tournament.id);
    option.textContent = `Общий турнир: ${tournament.name}`;
    option.dataset.templateKey = tournament.template_key;
    sourceInput.append(option);
  }
  sourceInput.value = state.draftSharedTournamentId
    ? String(state.draftSharedTournamentId)
    : "";
  const fieldLabel = createElement("span", {
    className: "form-field-label",
    text: "Название конкурса",
  });
  const input = createElement("input", {
    className: "text-input",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: "Параметры матчей и начисления зависят от выбранного шаблона.",
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
  input.placeholder = "Название конкурса";
  input.value = state.draftName || "";
  input.required = true;
  input.disabled = !hasTemplates;
  sourceInput.disabled = !hasTemplates;
  continueButton.disabled = !hasTemplates;

  function updateTemplateCopy() {
    if (!hasTemplates) {
      templateInput.disabled = true;
      sourceInput.disabled = true;
      input.disabled = true;
      continueButton.disabled = true;
      description.textContent = "Сейчас нет доступных шаблонов конкурса.";
      hint.textContent = "Новые шаблоны появятся в этом списке.";
      return;
    }

    const selectedSource = sourceInput.selectedOptions[0];
    const sharedTemplateKey = selectedSource?.dataset?.templateKey || "";
    if (sharedTemplateKey) {
      templateInput.value = sharedTemplateKey;
      templateInput.disabled = true;
    } else {
      templateInput.disabled = false;
    }
    const selectedTemplate = contestTemplates.find(
      (template) => template.key === templateInput.value,
    );
    const isTi = templateInput.value === "the_international_2026";
    const isWorldCup = templateInput.value === "world_cup_2026";
    if (sharedTemplateKey) {
      description.textContent = "Команды, матчи, дедлайны и результаты будут синхронизироваться с общим турниром.";
    } else if (isTi) {
      description.textContent = "Конкурс прогнозов на The International 2026.";
    } else if (isWorldCup) {
      description.textContent = "Конкурс прогнозов на Чемпионат мира 2026.";
    } else {
      description.textContent = selectedTemplate
        ? `Конкурс прогнозов по шаблону «${selectedTemplate.label}».`
        : "Выберите шаблон конкурса.";
    }
    if (isTi) {
      hint.textContent = "Серии плей-офф: 2 балла за точный счёт, 1 — за правильного победителя.";
    } else if (isWorldCup) {
      hint.textContent = "3 очка за точный счёт, 2 — за разницу голов, 1 — за исход.";
    } else {
      hint.textContent = "Параметры матчей и начисления зависят от выбранного шаблона.";
    }
  }
  templateInput.addEventListener("change", updateTemplateCopy);
  sourceInput.addEventListener("change", updateTemplateCopy);
  updateTemplateCopy();

  setFormMessage(
    message,
    state.formMessage || "",
    state.formMessageType || "",
  );

  sourceField.append(sourceLabel, sourceInput);
  templateField.append(templateLabel, templateInput);
  field.append(fieldLabel, input);
  actions.append(continueButton);
  form.append(sourceField, templateField, field, hint, message, actions);
  card.append(heading, description, form);

  form.addEventListener("submit", (event) => {
    event.preventDefault();

    if (!creatableTemplateKeys.has(templateInput.value)) {
      setFormMessage(message, "Сейчас нет доступных шаблонов.", "error");
      return;
    }

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
      draftTemplateKey: templateInput.value,
      draftSharedTournamentId: sourceInput.value
        ? Number(sourceInput.value)
        : null,
      idempotencyKey: createIdempotencyKey(),
    });
  });

  return card;
}

function createContestConfirmationCard(bootstrap, state) {
  const selectedTemplate = getCreatableContestTemplates(
    state.managementData?.contest_templates,
  ).find((template) => template.key === state.draftTemplateKey);
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
    text: "Проверьте название и выбранный шаблон конкурса.",
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

  if (!selectedTemplate) {
    details.textContent = "Выбранный шаблон больше недоступен для создания.";
    createButton.disabled = true;
  } else if (state.draftTemplateKey === "the_international_2026") {
    details.textContent = "Будет создан конкурс The International 2026 с прогнозом Swiss и сериями Bo3/Bo5.";
  } else if (state.draftTemplateKey === "world_cup_2026") {
    details.textContent = "Будет создан конкурс Чемпионата мира 2026 со стандартными футбольными правилами.";
  } else {
    details.textContent = `Будет создан конкурс по шаблону «${selectedTemplate.label}».`;
  }
  if (selectedTemplate && state.draftSharedTournamentId) {
    const tournament = state.managementData?.shared_tournaments?.find(
      (item) => item.id === state.draftSharedTournamentId,
    );
    details.textContent = (
      `Конкурс будет связан с общим турниром «${tournament?.name || "Без названия"}». `
      + "Расписание и результаты редактируются только в глобальном разделе."
    );
  }
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
      draftTemplateKey: state.draftTemplateKey,
      draftSharedTournamentId: state.draftSharedTournamentId,
    });
  });

  createButton.addEventListener("click", async () => {
    if (!selectedTemplate) {
      setFormMessage(
        message,
        "Выбранный шаблон больше недоступен для создания.",
        "error",
      );
      return;
    }
    createButton.disabled = true;
    backButton.disabled = true;
    createButton.textContent = "Создаём…";

    try {
      const result = await apiRequestForCurrentView("/api/tma/contests", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          [IDEMPOTENCY_KEY_HEADER]: state.idempotencyKey,
        },
        body: JSON.stringify({
          name: state.draftName,
          template_key: state.draftTemplateKey,
          shared_tournament_id: state.draftSharedTournamentId || null,
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

      if (error?.code === "template_unavailable") {
        const contestTemplates = getCreatableContestTemplates(
          state.managementData?.contest_templates,
        ).filter((template) => template.key !== state.draftTemplateKey);
        renderContestCreationState(bootstrap, {
          ...state,
          mode: "form",
          managementData: {
            ...(state.managementData || {}),
            contest_templates: contestTemplates,
          },
          draftTemplateKey: "",
          draftSharedTournamentId: null,
          idempotencyKey: "",
          formMessage: errorMessage,
          formMessageType: "error",
        });
        return;
      }

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
  setChatSummary();
  const creationState = {
    ...state,
    managementMode: true,
  };
  const formCard = state.mode === "confirm"
    ? createContestConfirmationCard(bootstrap, creationState)
    : createContestFormCard(bootstrap, creationState);

  replaceAppContent(
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
      const result = await apiRequestForCurrentView(
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
      await apiRequestForCurrentView(`/api/tma/contests/${contest.id}`, {
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
) {
  const entries = Array.isArray(leaderboard) ? leaderboard : [];

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
    const participantUsername =
      typeof entry?.participant_username === "string" &&
      entry.participant_username
        ? entry.participant_username
        : null;
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
    const calculatedPredictionsCount = Number.isSafeInteger(
      entry?.calculated_predictions_count,
    )
      ? entry.calculated_predictions_count
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
    const savedPredictionsCount =
      matchPredictionsCount +
      championPredictionCount +
      swissStagePredictionCount;
    const pendingPredictionsCount = Math.max(
      savedPredictionsCount - calculatedPredictionsCount,
      0,
    );

    const item = createElement("li", {
      className: "leaderboard-list-item",
    });
    const row = createLeaderboardRow(
      place,
      participantName,
      participantUsername,
      calculatedPredictionsCount,
      pendingPredictionsCount,
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
  participantUsername,
  calculatedPredictionsCount,
  pendingPredictionsCount,
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
  const identityElement = createElement("span", {
    className: "leaderboard-participant-identity",
  });
  const nameElement = createElement("span", {
    className: "leaderboard-participant-name",
    text: participantName,
  });
  identityElement.append(nameElement);
  if (participantUsername !== null) {
    identityElement.append(
      createElement("span", {
        className: "leaderboard-participant-username",
        text: `@${participantUsername}`,
      }),
    );
  }
  const predictionsElement = createElement("span", {
    className: "leaderboard-predictions",
    text: (
      `Прогнозов: ${calculatedPredictionsCount}+` +
      `${pendingPredictionsCount} из ` +
      `${calculatedPredictionsCount + pendingPredictionsCount}`
    ),
  });
  const pointsElement = createElement("span", {
    className: "leaderboard-points",
    text: `${totalPoints} ${getPointsLabel(totalPoints)}`,
  });

  participantElement.append(identityElement, predictionsElement);
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
      text: "Прогноз на швейцарскую систему",
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
      "Стыковой раунд",
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

function createContestRulesCard(
  templateKey,
  championPrediction,
  swissStagePrediction,
) {
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
  const isSeriesContest = templateKey === "the_international_2026";
  const isSwissStagePredictionEnabled =
    swissStagePrediction?.is_enabled === true;
  const championPoints = Number.isSafeInteger(championPrediction?.points)
    ? championPrediction.points
    : (isSeriesContest ? 4 : 5);
  let overviewText;
  if (isSeriesContest) {
    const tiOverviewParts = [];
    if (isSwissStagePredictionEnabled) {
      tiOverviewParts.push("Swiss 2/1");
    }
    if (isChampionPredictionEnabled) {
      tiOverviewParts.push(`чемпион +${championPoints}`);
    }
    tiOverviewParts.push("Double Elimination 2/1");
    overviewText = tiOverviewParts.join(" → ");
  } else {
    const footballOverview =
      "3 — счёт · 2 — разница · 1 — исход · +1 — победитель";
    overviewText = isChampionPredictionEnabled
      ? `${footballOverview} · +${championPoints} — чемпион`
      : footballOverview;
  }
  const overview = createElement("span", {
    className: "contest-rules-overview",
    text: overviewText,
  });
  const body = createElement("div", {
    className: "contest-rules-body",
  });

  card.className = "contest-rules-card";

  summaryContent.append(title, overview);
  summary.append(summaryContent);
  if (isSeriesContest) {
    if (isSwissStagePredictionEnabled) {
      body.append(
        createElement("p", {
          text: (
            "Швейцарский этап: 2 балла за команду и точный способ прохода, " +
            "1 балл — если способ прохода перепутан."
          ),
        }),
      );
    }
    if (isChampionPredictionEnabled) {
      body.append(
        createElement("p", {
          text: (
            "Прогноз на чемпиона открыт до старта Double Elimination. " +
            `За верно выбранного чемпиона — ещё ${championPoints} ` +
            `${getPointsLabel(championPoints)}.`
          ),
        }),
      );
    }
    body.append(
      createElement("p", {
        text: "Double Elimination: точный счёт серии — 2 балла. Верный победитель серии при другом счёте — 1 балл.",
      }),
      createElement("p", {
        text: "Серии Double Elimination играются до двух побед в Bo3 или до трёх побед в Bo5.",
      }),
    );
  } else {
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
    if (isSwissStagePredictionEnabled) {
      body.append(
        createElement("p", {
          text: (
            "Швейцарский этап: 2 балла за команду и точный способ прохода, " +
            "1 балл — если способ прохода перепутан."
          ),
        }),
      );
    }
  }

  if (isSeriesContest) {
    body.append(
      createElement("p", {
        text: "Прогноз на серию можно изменить до её начала.",
      }),
    );
  } else {
    body.append(
      createElement("p", {
        text: "Счёт учитывается после 90 или 120 минут. Голы серии пенальти в него не входят.",
      }),
      createElement("p", {
        text: "Прогноз на матч можно изменить до начала матча.",
      }),
    );
  }

  card.append(summary, body);
  return card;
}

function getActiveContestTab(tab) {
  return CONTEST_TABS.some((candidate) => candidate.id === tab)
    ? tab
    : "matches";
}

function getActiveContestManagementTab(tab) {
  return CONTEST_MANAGEMENT_TABS.some((candidate) => candidate.id === tab)
    ? tab
    : "matches";
}

function createTabNavigation(tabsConfig, activeTab, onSelectTab, ariaLabel) {
  const tabs = createElement("nav", {
    className: "contest-tabs",
  });
  const track = createElement("div", {
    className: (
      `contest-tabs-track contest-tabs-track--${tabsConfig.length}`
    ),
  });

  tabs.setAttribute("aria-label", ariaLabel);

  for (const tab of tabsConfig) {
    const button = createActionButton(tab.label, "contest-tab");
    const isActive = tab.id === activeTab;

    if (isActive) {
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

function createContestTabs(activeTab, onSelectTab) {
  return createTabNavigation(
    CONTEST_TABS,
    activeTab,
    onSelectTab,
    "Разделы конкурса",
  );
}

function createContestManagementTabs(activeTab, onSelectTab) {
  return createTabNavigation(
    CONTEST_MANAGEMENT_TABS,
    activeTab,
    onSelectTab,
    "Разделы управления конкурсом",
  );
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
  const summary = createElement("p", {
    className: "form-hint match-prediction-progress",
    text: "Прогнозы сохранены: 0 из 0.",
  });

  summary.setAttribute("role", "status");
  summary.setAttribute("aria-live", "polite");
  summary.setAttribute("aria-atomic", "true");
  return summary;
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
  const closed = statuses.filter(
    (status) => status.dataset.saveState === "closed",
  ).length;

  if (total === 0) {
    summary.textContent = "Нет матчей, для которых сейчас можно сделать прогноз.";
    return;
  }

  if (closed > 0) {
    summary.textContent = (
      `Прогнозы сохранены: ${saved} из ${total}. `
      + `Не сохранено до закрытия: ${closed}.`
    );
    return;
  }

  summary.textContent = saved === total
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
  const isSeries = Number.isSafeInteger(match.best_of);
  const section = createElement("div", {
    className: "match-result-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: isSeries ? "Результат серии" : "Результат матча",
  });

  if (contest.is_active === false || !canManageResults) {
    const readOnlyMessage = createElement("p", {
      className: "match-prediction-closed",
      text: result
        ? (
          isSeries
            ? `Итоговый счёт серии: ${result.home_score} : ${result.away_score}.`
            : (
              `Итоговый счёт: ${result.home_score} : ${result.away_score}. `
              + `Победитель противостояния: `
              + `${getTeamNameById(match, result.advancing_team_id)}.`
            )
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
    text: isSeries
      ? `Выберите официальный итоговый счёт Bo${match.best_of}. Результат можно исправить.`
      : (
        "Укажите итоговый счёт после 90 или 120 минут. " +
        "Голы серии пенальти в него не входят. Результат можно исправить."
      ),
  });
  const form = createElement("form", {
    className: "match-prediction-form",
  });
  const scoreHeading = createElement("p", {
    className: "form-field-label",
    text: isSeries ? "Итоговый счёт серии" : "Итоговый счёт матча",
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
  const seriesScoreField = createElement("label", {
    className: "form-field",
  });
  const seriesScoreLabel = createElement("span", {
    className: "form-field-label",
    text: "Счёт серии",
  });
  const seriesScoreInput = document.createElement("select");
  seriesScoreInput.className = "text-input";
  seriesScoreInput.id = `match-${match.id}-result-series-score`;
  seriesScoreInput.name = `match-${match.id}-result-series-score`;
  const emptySeriesScoreOption = document.createElement("option");
  emptySeriesScoreOption.value = "";
  emptySeriesScoreOption.textContent = "Выберите счёт";
  seriesScoreInput.append(emptySeriesScoreOption);
  if (isSeries) {
    for (const [homeScore, awayScore] of getSeriesScoreOptions(match.best_of)) {
      const option = document.createElement("option");
      option.value = `${homeScore}:${awayScore}`;
      option.textContent = `${match.home_team_name} ${homeScore}:${awayScore} ${match.away_team_name}`;
      seriesScoreInput.append(option);
    }
    seriesScoreInput.value = result
      ? `${result.home_score}:${result.away_score}`
      : "";
  }
  seriesScoreField.append(seriesScoreLabel, seriesScoreInput);
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

  const advancingTeamField = isSeries
    ? null
    : createAdvancingTeamField(match, {
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
    ...(isSeries
      ? [seriesScoreField]
      : [scoreGrid, advancingTeamField.element]),
    message,
    actions,
  );
  summary.append(summaryTitle, summaryAction);
  disclosure.append(summary, hint, form);
  section.append(heading, disclosure);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();

    let homeScore;
    let awayScore;
    let advancingTeamId = null;

    if (isSeries) {
      if (!seriesScoreInput.value) {
        seriesScoreInput.setAttribute("aria-invalid", "true");
        setFormMessage(
          message,
          `Выберите допустимый итоговый счёт Bo${match.best_of}.`,
          "error",
        );
        seriesScoreInput.focus();
        return;
      }
      [homeScore, awayScore] = seriesScoreInput.value.split(":").map(Number);
      seriesScoreInput.removeAttribute("aria-invalid");
    } else {
      const homeScoreValue = homeScoreInput.value.trim();
      const awayScoreValue = awayScoreInput.value.trim();
      homeScore = Number(homeScoreValue);
      awayScore = Number(awayScoreValue);

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
      advancingTeamId = advancingTeamField.getAdvancingTeamId();

      if (advancingTeamId === null) {
        setFormMessage(
          message,
          "При ничейном счёте выберите победителя противостояния.",
          "error",
        );
        advancingTeamField.focus();
        return;
      }
    }

    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";

    try {
      const resultPayload = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/matches/${match.id}/result`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            home_score: homeScore,
            away_score: awayScore,
            ...(advancingTeamId === null
              ? {}
              : { advancing_team_id: advancingTeamId }),
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

function getSeriesScoreOptions(bestOf) {
  const winsRequired = Math.floor(bestOf / 2) + 1;
  const options = [];
  for (let losingScore = 0; losingScore < winsRequired; losingScore += 1) {
    options.push([winsRequired, losingScore], [losingScore, winsRequired]);
  }
  return options;
}

function createMatchPredictionSection(contest, match) {
  const prediction = match.prediction;
  const isSeries = Number.isSafeInteger(match.best_of);
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
          isSeries
            ? `Ваш прогноз: ${prediction.home_score} : ${prediction.away_score}.`
            : (
              `Ваш прогноз: ${prediction.home_score} : ` +
              `${prediction.away_score}. Победитель противостояния: ` +
              `${getTeamNameById(match, prediction.advancing_team_id)}.`
            )
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
    text: isSeries
      ? `Выберите итоговый счёт Bo${match.best_of}. Прогноз сохранится автоматически.`
      : (
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
    text: isSeries ? "Итоговый счёт серии" : "Итоговый счёт матча",
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
  const seriesScoreField = createElement("label", {
    className: "form-field",
  });
  const seriesScoreLabel = createElement("span", {
    className: "form-field-label",
    text: "Счёт серии",
  });
  const seriesScoreInput = document.createElement("select");
  seriesScoreInput.className = "text-input";
  seriesScoreInput.id = `match-${match.id}-series-score`;
  seriesScoreInput.name = `match-${match.id}-series-score`;
  const emptySeriesScoreOption = document.createElement("option");
  emptySeriesScoreOption.value = "";
  emptySeriesScoreOption.textContent = "Выберите счёт";
  seriesScoreInput.append(emptySeriesScoreOption);
  if (isSeries) {
    for (const [homeScore, awayScore] of getSeriesScoreOptions(match.best_of)) {
      const option = document.createElement("option");
      option.value = `${homeScore}:${awayScore}`;
      option.textContent = `${match.home_team_name} ${homeScore}:${awayScore} ${match.away_team_name}`;
      seriesScoreInput.append(option);
    }
    seriesScoreInput.value = prediction
      ? `${prediction.home_score}:${prediction.away_score}`
      : "";
  }
  seriesScoreField.append(seriesScoreLabel, seriesScoreInput);
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

  saveStatus.setAttribute("role", "status");
  saveStatus.setAttribute("aria-live", "polite");
  saveStatus.setAttribute("aria-atomic", "true");

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
    if (isSeries) {
      if (!seriesScoreInput.value) {
        return {
          isReady: false,
          message: `Выберите допустимый итоговый счёт Bo${match.best_of}.`,
        };
      }
      const [homeScore, awayScore] = seriesScoreInput.value
        .split(":")
        .map(Number);
      return {
        isReady: true,
        predicted_home_score: homeScore,
        predicted_away_score: awayScore,
      };
    }
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
      payload.predicted_advancing_team_id || "",
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
  let deadlineTimer = null;

  function syncPredictionDeadline() {
    if (isMatchPredictionOpen(match)) {
      return false;
    }

    if (deadlineTimer !== null) {
      window.clearTimeout(deadlineTimer);
      deadlineTimer = null;
    }

    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }

    seriesScoreInput.disabled = true;
    homeScoreInput.disabled = true;
    awayScoreInput.disabled = true;
    advancingTeamField.element.disabled = true;
    form.setAttribute("aria-disabled", "true");

    const payload = readPredictionPayload();
    const isSaved = (
      payload.isReady
      && getPayloadFingerprint(payload) === lastSavedFingerprint
    );
    setSaveStatus(
      isSaved
        ? "Сохранено. Приём прогнозов завершён."
        : "Приём прогнозов завершён. Последние изменения не сохранены.",
      isSaved ? "saved" : "closed",
    );
    return true;
  }

  function schedulePredictionDeadlineSync() {
    if (deadlineTimer !== null) {
      window.clearTimeout(deadlineTimer);
      deadlineTimer = null;
    }
    const deadline = new Date(match.starts_at_utc).getTime();
    const remaining = deadline - Date.now();
    if (!Number.isFinite(remaining) || remaining <= 0) {
      syncPredictionDeadline();
      return;
    }
    deadlineTimer = window.setTimeout(() => {
      deadlineTimer = null;
      if (!form.isConnected || syncPredictionDeadline()) {
        return;
      }
      schedulePredictionDeadlineSync();
    }, Math.min(remaining, MAX_TIMER_DELAY_MS));
  }

  function scheduleSave() {
    if (syncPredictionDeadline()) {
      return;
    }
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
    if (syncPredictionDeadline()) {
      return;
    }
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
      const result = await queueMatchPredictionSave(
        contest.id,
        match.id,
        payload,
      );
      if (!result || !result.prediction) {
        throw new Error("Сервер вернул некорректный ответ при сохранении прогноза.");
      }

      match.prediction = result.prediction;
      lastSavedFingerprint = fingerprint;
    } catch (error) {
      isSaving = false;
      if (syncPredictionDeadline()) {
        return;
      }
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
    if (syncPredictionDeadline()) {
      return;
    }
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
    ...(isSeries ? [seriesScoreField] : [scoreGrid, advancingTeamField.element]),
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
  if (isSeries) {
    seriesScoreInput.addEventListener("change", scheduleSave);
  } else {
    homeScoreInput.addEventListener("input", scheduleSave);
    awayScoreInput.addEventListener("input", scheduleSave);
    advancingTeamField.element.addEventListener("change", scheduleSave);
  }
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }
    void savePrediction();
  });
  form.addEventListener(PREDICTION_FLUSH_EVENT, () => {
    if (deadlineTimer !== null) {
      window.clearTimeout(deadlineTimer);
      deadlineTimer = null;
    }
    if (saveTimer !== null) {
      window.clearTimeout(saveTimer);
      saveTimer = null;
    }
    void savePrediction();
  });
  form.dataset.predictionDeadline = match.starts_at_utc;
  form.addEventListener(
    PREDICTION_DEADLINE_SYNC_EVENT,
    () => {
      if (!syncPredictionDeadline()) {
        schedulePredictionDeadlineSync();
      }
    },
  );

  if (prediction) {
    setSaveStatus("Сохранено.", "saved");
  } else {
    setSaveStatus("", "draft");
  }
  if (!syncPredictionDeadline()) {
    schedulePredictionDeadlineSync();
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
      direct_qualifier_count: 3,
      elimination_qualifier_count: 5,
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
      : 3,
    elimination_qualifier_count: Number.isSafeInteger(
      prediction.elimination_qualifier_count,
    )
      ? prediction.elimination_qualifier_count
      : 5,
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
    status.textContent = "Не настроен";
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
        ? `Прогноз закрывается: ${formatMatchStartsAt(prediction.deadline_at)}`
        : "Время закрытия прогноза не задано.",
    }),
    createElement("p", {
      className: "match-meta",
      text: (
        `Напрямую: ${prediction.direct_qualifier_count}; ` +
        `через стыковой раунд: ` +
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
    successMessage = "",
    savedSubmitLabel = submitLabel,
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
      `Через стыковой раунд: ${eliminationIds.size} из ` +
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
      "Стыковой раунд",
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
      const result = await apiRequestForCurrentView(endpoint, {
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
      if (successMessage) {
        setFormMessage(message, successMessage, "success");
        submitButton.textContent = savedSubmitLabel;
        sync();
      }
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
        "Через стыковой раунд: " +
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
      ? "пройдёт через стыковой раунд"
      : "прошла через стыковой раунд";
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

function createSwissStagePredictionCard(contest) {
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
      text: "Прогноз на швейцарскую систему",
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
        onSaved: (savedPrediction) => {
          prediction.prediction = savedPrediction.prediction;
        },
        successMessage: "Прогноз сохранён.",
        savedSubmitLabel: "Изменить прогноз",
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

function getTournamentTeams(contest) {
  const tournamentTeams = contest?.tournament_teams;

  return {
    teams: Array.isArray(tournamentTeams?.teams)
      ? tournamentTeams.teams
      : [],
    is_locked: tournamentTeams?.is_locked === true,
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

function getDefaultDateTimeLocal(now = new Date()) {
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
    text: "Настройки прогноза",
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
    text: "Включить прогноз",
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
    "Сохранить настройки",
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
  const deadlineLocked = (
    championPrediction.is_enabled && !championPrediction.is_open
  );
  enabledInput.disabled = deadlineLocked;
  enabledField.append(enabledInput, enabledText);

  deadlineInput.id = `contest-${contest.id}-champion-deadline`;
  deadlineInput.name = `contest-${contest.id}-champion-deadline`;
  deadlineInput.type = "datetime-local";
  deadlineInput.step = "60";
  deadlineInput.value =
    formatDateTimeLocalValue(championPrediction.deadline_at)
    || getDefaultDateTimeLocal();

  pointsInput.id = `contest-${contest.id}-champion-points`;
  pointsInput.name = `contest-${contest.id}-champion-points`;
  pointsInput.type = "number";
  pointsInput.min = "0";
  pointsInput.step = "1";
  pointsInput.inputMode = "numeric";
  pointsInput.value = String(championPrediction.points);

  function syncEnabledState() {
    const isEnabled = enabledInput.checked;

    deadlineInput.disabled = !isEnabled || deadlineLocked;
    deadlineInput.required = isEnabled && !deadlineLocked;
    deadlineField.classList.toggle(
      "is-disabled",
      !isEnabled || deadlineLocked,
    );
    hint.hidden = !isEnabled;
    submitButton.textContent = "Сохранить настройки";
    if (isEnabled !== championPrediction.is_enabled) {
      submitButton.textContent = isEnabled
        ? "Включить прогноз"
        : "Выключить прогноз";
    }
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
      const result = await apiRequestForCurrentView(
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
      const result = await apiRequestForCurrentView(
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

      championPrediction.prediction = result.prediction;
      setFormMessage(message, "Прогноз сохранён.", "success");
      submitButton.textContent = "Изменить прогноз";
      submitButton.disabled = false;
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
      const result = await apiRequestForCurrentView(
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
      const result = await apiRequestForCurrentView(
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

function createPredictionReminderPublicationSection(contest) {
  const section = createElement("div", {
    className: "form-fields prediction-reminder-publication-section",
  });
  const hint = createElement("p", {
    className: "form-hint",
    text: (
      "Бот соберёт открытые прогнозы на швейцарский этап и чемпиона, "
      + "а также все ещё не начавшиеся матчи, и отправит одно сообщение."
    ),
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const publishButton = createActionButton(
    "Опубликовать предстоящие матчи",
    "secondary-action-button",
  );

  publishButton.addEventListener("click", async () => {
    publishButton.disabled = true;
    publishButton.textContent = "Собираем напоминания…";
    setFormMessage(message, "");

    try {
      const result = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/prediction-reminders/publish`,
        { method: "POST" },
      );
      if (result?.published !== true) {
        throw new Error(
          "Сервер вернул некорректный ответ при публикации напоминаний.",
        );
      }
      setFormMessage(message, "Напоминания опубликованы одним сообщением.", "success");
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error
          ? error.message
          : "Не удалось опубликовать напоминания.",
        "error",
      );
    } finally {
      publishButton.disabled = false;
      publishButton.textContent = "Опубликовать предстоящие матчи";
    }
  });

  actions.append(publishButton);
  section.append(hint, message, actions);
  return section;
}

function createIntermediateLeaderboardPublicationSection(contest) {
  const section = createElement("div", {
    className: "form-fields intermediate-leaderboard-publication-section",
  });
  const hasCalculatedPredictions = (
    Array.isArray(contest?.leaderboard)
    && contest.leaderboard.some(
      (entry) => Number(entry?.calculated_predictions_count) > 0,
    )
  );
  const hint = createElement("p", {
    className: "form-hint",
    text: hasCalculatedPredictions
      ? (
        "Бот отправит текущие места и очки участников. "
        + "Каждая публикация останется отдельным снимком рейтинга."
      )
      : "Публикация станет доступна после расчёта первого прогноза.",
  });
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const publishButton = createActionButton(
    "Опубликовать промежуточный рейтинг",
    "secondary-action-button",
  );
  let idempotencyKey = null;

  publishButton.disabled = !hasCalculatedPredictions;
  publishButton.addEventListener("click", async () => {
    idempotencyKey = idempotencyKey
      || createIdempotencyKey("leaderboard-publication");
    publishButton.disabled = true;
    publishButton.textContent = "Ставим в очередь…";
    setFormMessage(message, "");

    try {
      const result = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/leaderboard-publications`,
        {
          method: "POST",
          headers: {
            [IDEMPOTENCY_KEY_HEADER]: idempotencyKey,
          },
        },
      );
      if (result?.queued !== true) {
        throw new Error(
          "Сервер вернул некорректный ответ при публикации рейтинга.",
        );
      }
      idempotencyKey = null;
      setFormMessage(
        message,
        "Промежуточный рейтинг поставлен в очередь публикации.",
        "success",
      );
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error
          ? error.message
          : "Не удалось поставить рейтинг в очередь публикации.",
        "error",
      );
    } finally {
      publishButton.disabled = !hasCalculatedPredictions;
      publishButton.textContent = "Опубликовать промежуточный рейтинг";
    }
  });

  actions.append(publishButton);
  section.append(hint, message, actions);
  return section;
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
    createPredictionReminderPublicationSection(contest),
    createIntermediateLeaderboardPublicationSection(contest),
    createMatchPredictionPublicationSettingsDisclosure(
      contest,
      publication,
      onUpdated,
    ),
  );

  return item;
}

function createTournamentTeamsAdministrationCard(contest, state, onUpdated) {
  const tournamentTeams = getTournamentTeams(contest);
  const card = createElement("section", {
    className: "info-card tournament-teams-card",
  });
  const title = createElement("h3", { text: "Команды турнира" });
  const count = createElement("p", {
    className: "subtitle",
    text: `Сохранено команд: ${tournamentTeams.teams.length}.`,
  });
  const form = createElement("form", { className: "form-fields" });
  const field = createElement("label", { className: "form-field" });
  const input = document.createElement("textarea");
  input.className = "text-input swiss-stage-teams-input";
  input.rows = Math.min(
    12,
    Math.max(6, tournamentTeams.teams.length || 6),
  );
  input.placeholder = "По одной команде в строке";
  input.value = tournamentTeams.teams.map((team) => team.name).join("\n");
  input.disabled = tournamentTeams.is_locked;
  field.append(
    createElement("span", {
      className: "form-field-label",
      text: "Одна команда на строку",
    }),
    input,
  );
  const message = createElement("p", { className: "form-message" });
  setFormMessage(
    message,
    state.tournamentTeamsMessage || "",
    state.tournamentTeamsMessageType || "",
  );
  const actions = createElement("div", { className: "form-actions" });
  const submitButton = createActionButton(
    "Сохранить команды",
    "primary-action-button",
    "submit",
  );
  submitButton.disabled = tournamentTeams.is_locked;
  actions.append(submitButton);
  form.append(field);

  if (tournamentTeams.is_locked) {
    form.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: (
          "Список команд нельзя изменить после создания матчей, сохранения " +
          "прогнозов или внесения результатов."
        ),
      }),
    );
  } else {
    form.append(
      createElement("p", {
        className: "form-hint",
        text: "Пустые строки будут пропущены, порядок команд сохранится.",
      }),
    );
  }
  form.append(message, actions);
  card.append(title, count, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const teamNames = input.value.split(/\r?\n/);
    submitButton.disabled = true;
    submitButton.textContent = "Сохраняем…";
    try {
      const result = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/teams`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ team_names: teamNames }),
        },
      );
      if (!result?.tournament_teams) {
        throw new Error("Сервер вернул некорректный список команд.");
      }
      onUpdated(result.tournament_teams);
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error
          ? error.message
          : "Не удалось сохранить команды турнира.",
        "error",
      );
      submitButton.disabled = false;
      submitButton.textContent = "Сохранить команды";
    }
  });

  return card;
}

function createSwissStageSettingsForm(contest, prediction, onUpdated) {
  if (prediction.settings_locked && !prediction.is_open) {
    return createElement("p", {
      className: "match-prediction-closed",
      text: (
        "Настройки зафиксированы после сохранения первого пользовательского " +
        "прогноза или фактического результата, а дедлайн уже наступил."
      ),
    });
  }
  const disclosure = document.createElement("details");
  disclosure.className = "match-form-disclosure champion-settings-disclosure";
  const summary = document.createElement("summary");
  const summaryContent = createElement("div", {
    className: "match-form-summary-content",
  });
  const title = createElement("span", {
    className: "match-form-title",
    text: "Настройки прогноза",
  });
  const overview = createElement("span", {
    className: "match-form-overview",
    text: prediction.is_enabled
      ? "Срок и лимиты"
      : "Включить прогноз",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: prediction.settings_locked
      ? "До наступления текущего дедлайна его можно изменить. Остальные настройки зафиксированы."
      : (
        "Укажите, до какого времени можно выбрать команды и сколько команд пройдёт " +
        "напрямую и через стыковой раунд."
      ),
  });
  summaryContent.append(title, overview);
  summary.append(summaryContent);
  const form = createElement("form", { className: "form-fields" });
  const enabledField = createElement("label", {
    className: "champion-enable-option",
  });
  const enabledInput = document.createElement("input");
  enabledInput.type = "checkbox";
  enabledInput.checked = prediction.is_enabled;
  enabledInput.disabled = prediction.settings_locked;
  enabledField.append(
    enabledInput,
    createElement("span", { text: "Включить прогноз" }),
  );
  const deadlineField = createElement("label", { className: "form-field" });
  const deadlineInput = document.createElement("input");
  deadlineInput.className = "text-input";
  deadlineInput.type = "datetime-local";
  deadlineInput.step = "60";
  deadlineInput.value =
    formatDateTimeLocalValue(prediction.deadline_at)
    || getDefaultDateTimeLocal();
  deadlineField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Прогноз закрывается",
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
  directInput.disabled = prediction.settings_locked;
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
  eliminationInput.disabled = prediction.settings_locked;
  eliminationField.append(
    createElement("span", {
      className: "form-field-label",
      text: "Через стыковой раунд",
    }),
    eliminationInput,
  );
  limits.append(directField, eliminationField);
  const tournamentTeamsHint = createElement("p", {
    className: "form-hint",
    text: `Используются команды турнира: ${prediction.candidates.length}.`,
  });
  const message = createElement("p", { className: "form-message" });
  const actions = createElement("div", { className: "form-actions" });
  const submitButton = createActionButton(
    "Сохранить настройки",
    "primary-action-button",
    "submit",
  );
  function syncEnabledState() {
    const isEnabled = enabledInput.checked;
    const deadlineEditable = isEnabled && (
      !prediction.settings_locked || prediction.is_open
    );

    deadlineInput.disabled = !deadlineEditable;
    deadlineInput.required = deadlineEditable;
    deadlineField.classList.toggle("is-disabled", !deadlineEditable);
    submitButton.textContent = prediction.settings_locked
      ? "Сохранить дедлайн"
      : "Сохранить настройки";
    if (isEnabled !== prediction.is_enabled) {
      submitButton.textContent = isEnabled
        ? "Включить прогноз"
        : "Выключить прогноз";
    }
  }

  syncEnabledState();
  enabledInput.addEventListener("change", syncEnabledState);
  actions.append(submitButton);
  form.append(
    enabledField,
    deadlineField,
    limits,
    tournamentTeamsHint,
    message,
    actions,
  );
  disclosure.append(summary, description, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const directCount = Number(directInput.value);
    const eliminationCount = Number(eliminationInput.value);
    const deadline = new Date(deadlineInput.value);
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
      const result = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/swiss-stage-prediction/settings`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: enabledInput.checked,
            deadline_at: deadlineInput.value ? deadline.toISOString() : null,
            direct_qualifier_count: directCount,
            elimination_qualifier_count: eliminationCount,
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
      syncEnabledState();
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
      text: "Прогноз на швейцарскую систему",
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
        text: (
          "Включите прогноз, задайте срок закрытия и количество проходящих команд."
        ),
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

function createChampionAdministrationCard(contest, onUpdated) {
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
          "Включите прогноз, задайте срок закрытия и количество баллов. " +
          "Участники будут выбирать чемпиона на вкладке «Турнир»."
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

function createChampionPredictionCard(contest) {
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
      await apiRequestForCurrentView(
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
      await apiRequestForCurrentView(
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

function createMatchPredictionListItems(contest, matches) {
  return matches
    .map((match) => ({
      kind: "match",
      match,
      isOpen: isMatchPredictionOpen(match),
      sortTime: getPredictionSortTime(match.starts_at_utc),
      sortKey: `match-${String(match.id).padStart(12, "0")}`,
    }))
    .sort(comparePredictionListItems)
    .map((item) => createMatchListItem(
      contest,
      item.match,
      {},
      null,
      null,
      {
        showPredictions: true,
        showResults: false,
      },
    ));
}

function createTournamentPredictionListItems(contest) {
  const items = [];
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
    .map((item) => (
      item.kind === "champion"
        ? createChampionPredictionCard(contest)
        : createSwissStagePredictionCard(contest)
    ))
    .filter(Boolean);
}

function createContestPredictionSettingsCard(contest, state, onUpdated) {
  const card = createElement("section", {
    className: "info-card contest-prediction-settings-card",
  });
  const heading = createElement("h2", {
    text: "Прогнозы",
  });
  const list = createElement("ol", {
    className: "match-list",
  });

  list.append(
    createChampionAdministrationCard(contest, onUpdated),
    createSwissStageAdministrationCard(contest, onUpdated),
  );
  card.append(heading, list);
  return card;
}

function createContestPublicationsCard(contest, onUpdated) {
  const card = createElement("section", {
    className: "info-card contest-publications-card",
  });
  const heading = createElement("h2", {
    text: "Публикации",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Автоматические и ручные сообщения этого конкурса в Telegram-чате.",
  });
  const list = createElement("ol", {
    className: "match-list",
  });

  list.append(
    createMatchPredictionPublicationAdministrationCard(contest, onUpdated),
  );
  card.append(heading, description, list);
  return card;
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
    listItems = null,
  },
) {
  const normalizedListItems = Array.isArray(listItems)
    ? listItems.filter(Boolean)
    : null;
  const visibleMessage = showResults ? state.matchesMessage || "" : "";
  const visibleMessageType = showResults
    ? state.matchesMessageType || ""
    : "";
  const hasListItems = normalizedListItems !== null
    ? normalizedListItems.length > 0
    : matches.length > 0;

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

  if (!hasListItems) {
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
  const isSeriesContest = contest.template_key === "the_international_2026";
  const tournamentTeams = getTournamentTeams(contest).teams;
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
    text: isSeriesContest ? "Добавить серию" : "Добавить матч",
  });
  const overview = createElement("span", {
    className: "match-form-overview",
    text: isSeriesContest
      ? "Укажите команды, формат и время начала"
      : "Укажите команды и время начала",
  });
  const description = createElement("p", {
    className: "subtitle",
    text: isSeriesContest
      ? "Укажите команды, формат и официальное время начала серии."
      : "Укажите команды и время начала матча.",
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
  const homeTeamInput = createChampionTeamSelect(tournamentTeams, {
    id: "match-home-team-id",
    name: "match-home-team-id",
    selectedTeamId: draft.homeTeamId ?? null,
  });
  const awayTeamField = createElement("label", {
    className: "form-field",
  });
  const awayTeamLabel = createElement("span", {
    className: "form-field-label",
    text: "Вторая команда",
  });
  const awayTeamInput = createChampionTeamSelect(tournamentTeams, {
    id: "match-away-team-id",
    name: "match-away-team-id",
    selectedTeamId: draft.awayTeamId ?? null,
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
  const bestOfField = createElement("label", {
    className: "form-field",
  });
  const bestOfLabel = createElement("span", {
    className: "form-field-label",
    text: "Формат серии",
  });
  const bestOfInput = document.createElement("select");
  bestOfInput.className = "text-input";
  bestOfInput.id = "match-best-of";
  bestOfInput.name = "match-best-of";
  for (const value of [3, 5]) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = `Bo${value}`;
    bestOfInput.append(option);
  }
  bestOfInput.value = String(draft.bestOf || 3);
  bestOfField.append(bestOfLabel, bestOfInput);
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
    isSeriesContest ? "Добавить серию" : "Добавить матч",
    "primary-action-button",
    "submit",
  );

  disclosure.className = "match-form-disclosure";
  disclosure.open = Boolean(state.matchFormMessage || state.matchDraft);
  const wasInitiallyOpen = disclosure.open;
  summaryContent.append(title, overview);
  summary.append(summaryContent);

  homeTeamInput.required = true;

  awayTeamInput.required = true;

  startsAtInput.id = "match-starts-at";
  startsAtInput.name = "match-starts-at";
  startsAtInput.type = "datetime-local";
  startsAtInput.step = "60";
  startsAtInput.value =
    draft.startsAtLocal || getDefaultDateTimeLocal();
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
  if (tournamentTeams.length < 2) {
    homeTeamInput.disabled = true;
    awayTeamInput.disabled = true;
    startsAtInput.disabled = true;
    submitButton.disabled = true;
    setFormMessage(
      message,
      "Сначала добавьте как минимум две команды турнира.",
      "error",
    );
  }
  form.append(
    homeTeamField,
    awayTeamField,
    ...(isSeriesContest ? [bestOfField] : []),
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

    const homeTeamId = Number(homeTeamInput.value);
    const awayTeamId = Number(awayTeamInput.value);
    const startsAtLocal = startsAtInput.value;
    const startsAt = new Date(startsAtLocal);
    const bestOf = isSeriesContest ? Number(bestOfInput.value) : null;

    if (!Number.isSafeInteger(homeTeamId) || homeTeamId <= 0) {
      homeTeamInput.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Выберите первую команду.", "error");
      homeTeamInput.focus();
      return;
    }

    homeTeamInput.removeAttribute("aria-invalid");

    if (!Number.isSafeInteger(awayTeamId) || awayTeamId <= 0) {
      awayTeamInput.setAttribute("aria-invalid", "true");
      setFormMessage(message, "Выберите вторую команду.", "error");
      awayTeamInput.focus();
      return;
    }

    awayTeamInput.removeAttribute("aria-invalid");

    if (homeTeamId === awayTeamId) {
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
      homeTeamId,
      awayTeamId,
      startsAtLocal,
      bestOf,
    };
    const idempotencyKey =
      state.matchIdempotencyKey || createIdempotencyKey("match");

    submitButton.disabled = true;
    submitButton.textContent = "Добавляем…";

    try {
      const result = await apiRequestForCurrentView(
        `/api/tma/contests/${contest.id}/matches`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            [IDEMPOTENCY_KEY_HEADER]: idempotencyKey,
          },
          body: JSON.stringify({
            home_team_id: homeTeamId,
            away_team_id: awayTeamId,
            starts_at_utc: startsAt.toISOString(),
            ...(isSeriesContest ? { best_of: bestOf } : {}),
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
      void openContest(bootstrap, contest.id, {
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
  return replaceAppContent(
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
  replaceAppContent(card);
}

function renderContestDetailsScreen(bootstrap, contest, state = {}) {
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
      createLeaderboardCard(leaderboard),
    );
  } else if (activeTab === "tournament") {
    cards.push(
      createContestRulesCard(
        contest.template_key,
        contest.champion_prediction,
        contest.swiss_stage_prediction,
      ),
      createMatchesCard(
        contest,
        [],
        state,
        null,
        null,
        {
          title: "Турнир",
          emptyMessages: [
            "Дополнительные прогнозы для этого конкурса не настроены.",
          ],
          showPredictions: true,
          showResults: false,
          canManageResults: false,
          listItems: createTournamentPredictionListItems(contest),
        },
      ),
    );
  } else {
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
            ? [
              "Матчей пока нет.",
              "Когда кто-то добавит матч, здесь можно будет сохранить прогноз.",
            ]
            : ["Матчей нет."],
          showPredictions: true,
          showResults: false,
          canManageResults: false,
          listItems: createMatchPredictionListItems(contest, matches),
        },
      ),
    );
  }

  setChatSummary(`Привет, ${userName}. Чат «${chatTitle}».`);
  replaceAppContent(...cards);
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
  setChatSummary();
  const matches = Array.isArray(contest.matches) ? contest.matches : [];
  const isActive = contest.is_active !== false;
  const isSharedTournament = contest.shared_tournament !== null;
  const activeTab = getActiveContestManagementTab(state.managementTab);
  const managementState = {
    ...state,
    managementMode: true,
    managementTab: activeTab,
  };
  const cards = [
    createContestManagementHeader(contest, bootstrap),
    createContestManagementTabs(activeTab, (nextTab) => {
      if (nextTab === activeTab) {
        return;
      }

      void openContest(bootstrap, contest.id, {
        ...managementState,
        managementTab: nextTab,
      });
    }),
  ];

  if (activeTab === "settings") {
    if (!isActive) {
      cards.push(
        createInfoCard(
          "Настройки",
          ["Конкурс завершён. Настройки доступны только для просмотра в конкурсе."],
        ),
      );
    } else {
      if (isSharedTournament) {
        cards.push(
          createInfoCard(
            "Общий турнир",
            [
              `Конкурс связан с турниром «${contest.shared_tournament.name}». `
                + "Команды, матчи, дедлайны и результаты редактируются только в глобальном разделе.",
            ],
          ),
        );
      } else {
        cards.push(
          createTournamentTeamsAdministrationCard(
            contest,
            managementState,
            () => {
              void openContest(bootstrap, contest.id, {
                ...managementState,
                tournamentTeamsMessage: "Команды турнира сохранены.",
                tournamentTeamsMessageType: "success",
              });
            },
          ),
        );
      }
      if (!isSharedTournament) {
        cards.push(
          createContestPredictionSettingsCard(
            contest,
            managementState,
            () => {
              void openContest(bootstrap, contest.id, managementState);
            },
          ),
        );
      }
      cards.push(
        createContestCompletionCard(bootstrap, contest, managementState),
        createContestDeletionCard(bootstrap, contest, managementState),
      );
    }
  } else if (activeTab === "publications") {
    if (!isActive) {
      cards.push(
        createInfoCard(
          "Публикации",
          ["Конкурс завершён. Управление публикациями недоступно."],
        ),
      );
    } else {
      cards.push(
        createContestPublicationsCard(contest, () => {
          void openContest(bootstrap, contest.id, managementState);
        }),
      );
    }
  } else {
    if (isActive && !isSharedTournament) {
      cards.push(createMatchFormCard(bootstrap, contest, managementState));
    }
    if (isActive && isSharedTournament) {
      cards.push(
        createInfoCard(
          "Синхронизируемое расписание",
          [
            `Матчи и результаты приходят из общего турнира «${contest.shared_tournament.name}».`,
          ],
        ),
      );
    }
    cards.push(
      createMatchesCard(
        contest,
        matches,
        managementState,
        isSharedTournament ? null : (resultState) => {
          void openContest(bootstrap, contest.id, {
            ...managementState,
            resultMatchId: resultState.matchId,
            resultMessage: resultState.message,
            resultMessageType: resultState.type,
          });
        },
        isSharedTournament ? null : (deletionState) => {
          void openContest(bootstrap, contest.id, {
            ...managementState,
            ...deletionState,
          });
        },
        {
          title: "Матчи",
          emptyMessages: isActive
            ? ["Матчей пока нет.", "Добавьте первый матч выше."]
            : ["Матчей нет."],
          showPredictions: false,
          showResults: true,
          canManageResults: isActive && !isSharedTournament,
        },
      ),
    );
  }

  replaceAppContent(...cards);
}

function renderContestManagementError(bootstrap, message) {
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
  replaceAppContent(
    createManagementHeaderCard(bootstrap),
    card,
  );
}

async function openContest(bootstrap, contestId, state = {}) {
  const viewToken = renderContestDetailsLoading(bootstrap);

  try {
    await waitForMatchPredictionSaves(contestId);
    if (!isCurrentView(viewToken)) {
      return;
    }
    const path = state.managementMode === true
      ? `/api/tma/management/contests/${contestId}`
      : `/api/tma/contests/${contestId}`;
    const result = await apiRequestForCurrentView(path);

    if (!isCurrentView(viewToken)) {
      return;
    }

    if (!result || !result.contest) {
      throw new Error("Сервер вернул некорректный ответ при открытии конкурса.");
    }

    if (state.managementMode === true) {
      renderContestManagementScreen(bootstrap, result.contest, state);
    } else {
      renderContestDetailsScreen(bootstrap, result.contest, state);
    }
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
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
    + `через стыковой раунд: ${formatTeams(eliminationTeamIds)}`
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
    case "chat_settings_updated":
      return [
        `Текст кнопки: «${before.app_button_text || ""}» → «${after.app_button_text || ""}».`,
      ];
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
    case "tournament_teams_updated":
      return [
        "Команды турнира: "
        + `${formatAuditTeamList(before.teams)} → `
        + `${formatAuditTeamList(after.teams)}.`,
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
    case "intermediate_leaderboard_publication_requested":
      return [
        `Запрошена публикация промежуточного рейтинга конкурса «${entityName}».`,
      ];
    case "supermoderator_assigned":
      return [`Пользователю ${entityName} назначена роль супермодератора.`];
    case "supermoderator_revoked":
      return [`У пользователя ${entityName} отозвана роль супермодератора.`];
    default:
      return ["Сохранено административное действие."];
  }
}

function formatAuditTeamList(teams) {
  if (!Array.isArray(teams) || teams.length === 0) {
    return "нет";
  }
  return teams
    .map((team) => (
      team && typeof team.name === "string" ? team.name : `#${team?.id || "?"}`
    ))
    .join(", ");
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
    elimination_qualifier_count: "Через стыковой раунд",
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
  setChatSummary();
  return replaceAppContent(
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
  const viewToken = renderAuditScreen(bootstrap, state);

  let managementRequestFailed = false;
  try {
    const result = await apiRequestForCurrentView(buildAuditRequestPath(state, append));
    if (!isCurrentView(viewToken)) {
      return;
    }
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
    if (!isCurrentView(viewToken)) {
      return;
    }
    if (handleManagementRequestError(error)) {
      managementRequestFailed = true;
      return;
    }
    state.error = getAuditErrorMessage(error);
    state.initialized = true;
  } finally {
    state.loading = false;
    if (!managementRequestFailed && isCurrentView(viewToken)) {
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
      const result = await apiRequestForCurrentView("/api/tma/access/supermoderators");
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
    preview.replaceChildren();
    input.disabled = true;
    findButton.disabled = true;
    findButton.textContent = "Ищем…";
    setFormMessage(formMessage, "Ищем пользователя в Telegram…");
    try {
      const result = await apiRequestForCurrentView("/api/tma/access/users/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ target }),
      });
      setFormMessage(formMessage, "");
      renderResolvedSupermoderator(
        preview,
        result,
        async (selectedUser) => {
          activeOperation = "assign";
          input.disabled = true;
          findButton.disabled = true;
          findButton.textContent = "Назначаем…";
          try {
            await apiRequestForCurrentView(
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
  setChatSummary();
  replaceAppContent(
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
        await apiRequestForCurrentView(
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

function canManageContests(bootstrap) {
  return bootstrap.access?.can_manage_contests === true;
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

function createChatSettingsCard(bootstrap, managementData, state = {}) {
  const settings = managementData.chat_settings || {};
  const card = createElement("section", {
    className: "info-card chat-settings-card",
  });
  const heading = createElement("h2", { text: "Кнопка открытия Клевера" });
  const description = createElement("p", {
    className: "subtitle",
    text: "Этот текст появится на кнопке в новых сообщениях, отправленных командой /app.",
  });
  const form = createElement("form", { className: "form-fields" });
  const field = createElement("label", { className: "form-field" });
  const label = createElement("span", {
    className: "form-field-label",
    text: "Текст кнопки",
  });
  const input = createElement("input", { className: "text-input" });
  const hint = createElement("p", {
    className: "form-hint",
    text: "От 1 до 64 символов. Уже отправленные сообщения не изменятся.",
  });
  const message = createElement("p", { className: "form-message" });
  const submitButton = createActionButton(
    "Сохранить",
    "primary-action-button",
    "submit",
  );

  input.type = "text";
  input.name = "app-button-text";
  input.maxLength = 64;
  input.required = true;
  input.autocomplete = "off";
  input.value = settings.app_button_text || "Открыть Клевер";
  setFormMessage(
    message,
    state.chatSettingsMessage || "",
    state.chatSettingsMessageType || "",
  );

  field.append(label, input, hint);
  form.append(field, submitButton, message);
  card.append(heading, description, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const appButtonText = input.value.trim();
    if (!appButtonText) {
      setFormMessage(message, "Введите текст кнопки.", "error");
      return;
    }
    submitButton.disabled = true;
    input.disabled = true;
    submitButton.textContent = "Сохраняем…";
    try {
      const result = await apiRequestForCurrentView("/api/tma/management/chat-settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ app_button_text: appButtonText }),
      });
      renderManagementScreen(
        bootstrap,
        { ...managementData, chat_settings: result.chat_settings },
        {
          ...state,
          chatSettingsMessage: "Настройка сохранена.",
          chatSettingsMessageType: "success",
        },
      );
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      setFormMessage(
        message,
        error instanceof Error ? error.message : "Не удалось сохранить настройку.",
        "error",
      );
      submitButton.disabled = false;
      input.disabled = false;
      submitButton.textContent = "Сохранить";
    }
  });

  return card;
}

function createSharedTournamentNavigationCard(bootstrap, managementData) {
  const tournaments = Array.isArray(managementData.shared_tournaments)
    ? managementData.shared_tournaments
    : [];
  const card = createElement("section", {
    className: "info-card management-access-card",
  });
  const heading = createElement("h2", { text: "Общие турниры" });
  const description = createElement("p", {
    className: "subtitle",
    text: (
      "Единое расписание и результаты для конкурсов в разных чатах. "
      + `${tournaments.length} ${getRussianPlural(tournaments.length, "турнир", "турнира", "турниров")}.`
    ),
  });
  const button = createActionButton(
    "Открыть общие турниры",
    "secondary-action-button",
  );
  button.addEventListener("click", () => {
    void openSharedTournamentManagement(bootstrap);
  });
  card.append(heading, description, button);
  return card;
}

function createSharedTournamentListCard(bootstrap, tournaments, state = {}) {
  const card = createElement("section", {
    className: "info-card management-contest-list-card",
  });
  const heading = createElement("h2", { text: "Общие турниры" });
  const message = createElement("p", { className: "form-message" });
  setFormMessage(message, state.message || "", state.messageType || "");
  card.append(
    heading,
    createElement("p", {
      className: "subtitle",
      text: "Изменения матчей применяются ко всем связанным конкурсам.",
    }),
    message,
  );

  if (tournaments.length === 0) {
    card.append(
      createElement("p", {
        className: "subtitle",
        text: "Общих турниров пока нет.",
      }),
    );
    return card;
  }

  const groups = [
    ["Активные", tournaments.filter((tournament) => tournament.is_archived !== true)],
    ["Завершённые", tournaments.filter((tournament) => tournament.is_archived === true)],
  ];
  for (const [label, group] of groups) {
    if (group.length === 0) {
      continue;
    }
    card.append(createElement("h3", { text: label }));
    const list = createElement("ul", { className: "management-navigation-list" });
    for (const tournament of group) {
      const item = createElement("li", { className: "management-list-item" });
      const button = createManagementNavigationRow(
        tournament.name,
        (
          `${tournament.match_count} ${getRussianPlural(tournament.match_count, "матч", "матча", "матчей")}, `
          + `${tournament.linked_contest_count} ${getRussianPlural(tournament.linked_contest_count, "конкурс", "конкурса", "конкурсов")}`
        ),
        () => {
          void openSharedTournament(bootstrap, tournament.id);
        },
      );
      item.append(button);
      list.append(item);
    }
    card.append(list);
  }
  return card;
}

function createSharedTournamentCreationCard(
  bootstrap,
  contestTemplatesValue,
  state = {},
) {
  const contestTemplates = getCreatableContestTemplates(contestTemplatesValue);
  const hasTemplates = contestTemplates.length > 0;
  const card = createElement("section", {
    className: "info-card contest-form-card",
  });
  const heading = createElement("h2", { text: "Создать общий турнир" });
  const form = createElement("form", { className: "form-fields" });
  const nameField = createElement("label", { className: "form-field" });
  const nameInput = createElement("input", { className: "text-input" });
  nameInput.type = "text";
  nameInput.required = true;
  nameInput.maxLength = CONTEST_NAME_MAX_LENGTH;
  nameInput.placeholder = "Название общего турнира";
  nameInput.disabled = !hasTemplates;
  nameField.append(
    createElement("span", { className: "form-field-label", text: "Название" }),
    nameInput,
  );
  const templateField = createElement("label", { className: "form-field" });
  const templateInput = document.createElement("select");
  templateInput.className = "text-input";
  fillContestTemplateSelect(templateInput, contestTemplates);
  templateField.append(
    createElement("span", { className: "form-field-label", text: "Шаблон" }),
    templateInput,
  );
  const message = createElement("p", { className: "form-message" });
  setFormMessage(
    message,
    state.creationMessage || "",
    state.creationMessageType || "",
  );
  const submitButton = createActionButton(
    "Создать турнир",
    "primary-action-button",
    "submit",
  );
  submitButton.disabled = !hasTemplates;
  form.append(nameField, templateField, submitButton, message);
  card.append(heading, form);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!hasTemplates || !templateInput.value) {
      setFormMessage(message, "Сейчас нет доступных шаблонов.", "error");
      return;
    }
    const name = normalizeContestName(nameInput.value);
    if (!name) {
      setFormMessage(message, "Введите название общего турнира.", "error");
      return;
    }
    submitButton.disabled = true;
    try {
      const result = await apiRequestForCurrentView("/api/tma/shared-tournaments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, template_key: templateInput.value }),
      });
      void openSharedTournament(bootstrap, result.shared_tournament.id);
    } catch (error) {
      if (handleManagementRequestError(error)) {
        return;
      }
      if (error?.code === "template_unavailable") {
        void openSharedTournamentManagement(bootstrap, {
          creationMessage: error instanceof Error
            ? error.message
            : "Выбранный шаблон больше недоступен для создания.",
          creationMessageType: "error",
        });
        return;
      }
      setFormMessage(
        message,
        error instanceof Error ? error.message : "Не удалось создать турнир.",
        "error",
      );
      submitButton.disabled = !hasTemplates;
    }
  });
  return card;
}

function renderSharedTournamentManagementScreen(
  bootstrap,
  tournaments,
  contestTemplates,
  state = {},
) {
  setChatSummary();
  replaceAppContent(
    createAdministrativeHeader(bootstrap, {
      title: "Общие турниры",
      backLabel: "← К управлению",
      onBack: () => {
        void openManagement(bootstrap);
      },
      description: "Глобальный раздел: изменения не ограничены текущим чатом.",
    }),
    createSharedTournamentListCard(bootstrap, tournaments, state),
    createSharedTournamentCreationCard(bootstrap, contestTemplates, state),
  );
}

async function openSharedTournamentManagement(bootstrap, state = {}) {
  activeBootstrap = bootstrap;
  const viewToken = replaceAppContent(
    createStatusCard("Открываем общие турниры", "Загружаем расписания…"),
  );
  try {
    const result = await apiRequestForCurrentView("/api/tma/shared-tournaments");
    if (!isCurrentView(viewToken)) {
      return;
    }
    renderSharedTournamentManagementScreen(
      bootstrap,
      Array.isArray(result.shared_tournaments) ? result.shared_tournaments : [],
      Array.isArray(result.contest_templates) ? result.contest_templates : [],
      state,
    );
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
    if (handleManagementRequestError(error)) {
      return;
    }
    renderSharedTournamentManagementScreen(
      bootstrap,
      [],
      [],
      {
        message: error instanceof Error ? error.message : "Не удалось загрузить турниры.",
        messageType: "error",
      },
    );
  }
}

function createSharedTournamentLifecycleCard(bootstrap, tournament) {
  const card = createElement("section", { className: "info-card" });
  const message = createElement("p", { className: "form-message" });
  if (tournament.is_archived === true) {
    const restoreButton = createActionButton(
      "Вернуть в активные",
      "secondary-action-button",
    );
    restoreButton.addEventListener("click", async () => {
      restoreButton.disabled = true;
      try {
        await apiRequestForCurrentView(
          `/api/tma/shared-tournaments/${tournament.id}/restore`,
          {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ expected_version: tournament.version }),
          },
        );
        void openSharedTournament(bootstrap, tournament.id, {
          message: "Общий турнир возвращён в активные.",
          messageType: "success",
        });
      } catch (error) {
        setFormMessage(
          message,
          error instanceof Error ? error.message : "Не удалось восстановить турнир.",
          "error",
        );
        restoreButton.disabled = false;
      }
    });
    card.append(
      createElement("h2", { text: "Турнир завершён" }),
      createElement("p", {
        className: "subtitle",
        text: (
          "Данные доступны только для просмотра. Для позднего исправления "
          + "результатов верните турнир в активные."
        ),
      }),
      restoreButton,
      message,
    );
    return card;
  }

  const archiveButton = createActionButton(
    "Завершить общий турнир",
    "danger-action-button",
  );
  archiveButton.addEventListener("click", async () => {
    const confirmed = window.confirm(
      "Завершить общий турнир? Он станет доступен только для просмотра, "
      + "а автоматическая синхронизация расписания остановится. Связанные "
      + "конкурсы нужно завершить отдельно.",
    );
    if (!confirmed) {
      return;
    }
    archiveButton.disabled = true;
    try {
      await apiRequestForCurrentView(
        `/api/tma/shared-tournaments/${tournament.id}/archive`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_version: tournament.version }),
        },
      );
      void openSharedTournament(bootstrap, tournament.id, {
        message: "Общий турнир завершён и перемещён в архив.",
        messageType: "success",
      });
    } catch (error) {
      setFormMessage(
        message,
        error instanceof Error ? error.message : "Не удалось завершить турнир.",
        "error",
      );
      archiveButton.disabled = false;
    }
  });
  card.append(
    createElement("h2", { text: "Завершить общий турнир" }),
    createElement("p", {
      className: "subtitle",
      text: (
        "Завершение доступно после внесения результатов всех матчей и "
        + "включённых долгосрочных прогнозов. Конкурсы в чатах останутся активными."
      ),
    }),
    archiveButton,
    message,
  );
  return card;
}

function createSharedTournamentTeamsCard(bootstrap, tournament, state = {}) {
  const card = createElement("section", { className: "info-card" });
  const heading = createElement("h2", { text: "Команды" });
  if (tournament.is_archived === true) {
    const teamNames = tournament.teams.map((team) => team.name).join(", ");
    card.append(
      heading,
      createElement("p", {
        className: "subtitle",
        text: teamNames || "Команды не указаны.",
      }),
    );
    return card;
  }
  const form = createElement("form", { className: "form-fields" });
  const field = createElement("label", { className: "form-field" });
  const input = document.createElement("textarea");
  input.className = "text-input teams-textarea";
  input.rows = Math.max(4, Math.min(12, tournament.teams.length + 1));
  input.value = tournament.teams.map((team) => team.name).join("\n");
  field.append(
    createElement("span", {
      className: "form-field-label",
      text: "По одной команде в строке",
    }),
    input,
  );
  const message = createElement("p", { className: "form-message" });
  setFormMessage(message, state.teamsMessage || "", state.teamsMessageType || "");
  const submitButton = createActionButton(
    "Сохранить команды",
    "primary-action-button",
    "submit",
  );
  form.append(field, submitButton, message);
  card.append(heading, form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const teamNames = input.value
      .split("\n")
      .map(normalizeTeamName)
      .filter(Boolean);
    submitButton.disabled = true;
    try {
      await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournament.id}/teams`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          team_names: teamNames,
          expected_version: tournament.version,
        }),
      });
      void openSharedTournament(bootstrap, tournament.id, {
        teamsMessage: "Команды сохранены.",
        teamsMessageType: "success",
      });
    } catch (error) {
      setFormMessage(
        message,
        error instanceof Error ? error.message : "Не удалось сохранить команды.",
        "error",
      );
      submitButton.disabled = false;
    }
  });
  return card;
}

function createSharedChampionCard(bootstrap, tournament) {
  const card = createElement("section", { className: "info-card" });
  card.append(createElement("h2", { text: "Прогноз на чемпиона" }));
  const settings = tournament.champion_prediction || {};
  if (tournament.is_archived === true) {
    const lines = settings.is_enabled
      ? [
          `Дедлайн: ${formatMatchStartsAt(settings.deadline_at)}`,
          `Баллы: ${settings.points ?? 0}`,
          `Фактический чемпион: ${settings.actual_champion?.name || "не указан"}`,
        ]
      : ["Прогноз не был включён."];
    for (const line of lines) {
      card.append(createElement("p", { className: "subtitle", text: line }));
    }
    return card;
  }
  const form = createElement("form", { className: "form-fields" });
  const enabled = createElement("input");
  enabled.type = "checkbox";
  enabled.checked = settings.is_enabled === true;
  const deadline = createElement("input", { className: "text-input" });
  deadline.type = "datetime-local";
  deadline.value = settings.deadline_at
    ? formatDateTimeLocalValue(settings.deadline_at)
    : "";
  const points = createElement("input", { className: "text-input" });
  points.type = "number";
  points.min = "0";
  points.value = String(settings.points ?? 5);
  const message = createElement("p", { className: "form-message" });
  const saveButton = createActionButton("Сохранить общие настройки", "primary-action-button", "submit");
  form.append(
    createLabeledField("Включить прогноз", enabled),
    createLabeledField("Дедлайн", deadline),
    createLabeledField("Баллы", points),
    saveButton,
    message,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    saveButton.disabled = true;
    try {
      await apiRequestForCurrentView(
        `/api/tma/shared-tournaments/${tournament.id}/champion-prediction/settings`,
        {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            enabled: enabled.checked,
            deadline_at: enabled.checked && deadline.value
              ? new Date(deadline.value).toISOString()
              : null,
            points: Number(points.value),
            expected_version: tournament.version,
          }),
        },
      );
      void openSharedTournament(bootstrap, tournament.id, {
        message: "Настройки прогноза на чемпиона обновлены во всех чатах.",
        messageType: "success",
      });
    } catch (error) {
      setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
      saveButton.disabled = false;
    }
  });
  card.append(form);

  const deadlinePassed = settings.deadline_at
    && new Date(settings.deadline_at).getTime() <= Date.now();
  const matchesFinished = (tournament.matches || []).every(
    (match) => ["finished", "cancelled"].includes(match.status),
  );
  if (settings.is_enabled && deadlinePassed && matchesFinished && tournament.teams.length) {
    const resultForm = createElement("form", { className: "form-fields" });
    const champion = document.createElement("select");
    champion.className = "text-input";
    for (const team of tournament.teams) {
      const option = document.createElement("option");
      option.value = String(team.id);
      option.textContent = team.name;
      champion.append(option);
    }
    if (settings.actual_champion) {
      champion.value = String(settings.actual_champion.id);
    }
    const resultButton = createActionButton(
      settings.actual_champion ? "Исправить чемпиона во всех чатах" : "Указать чемпиона во всех чатах",
      "secondary-action-button",
      "submit",
    );
    resultForm.append(createLabeledField("Фактический чемпион", champion), resultButton);
    resultForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      resultButton.disabled = true;
      try {
        await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournament.id}/champion`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            champion_team_id: Number(champion.value),
            expected_version: tournament.version,
          }),
        });
        void openSharedTournament(bootstrap, tournament.id, {
          message: "Чемпион и рейтинги обновлены во всех чатах.",
          messageType: "success",
        });
      } catch (error) {
        setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
        resultButton.disabled = false;
      }
    });
    card.append(resultForm);
  }
  return card;
}

function createSharedSwissStageCard(bootstrap, tournament) {
  const card = createElement("section", { className: "info-card" });
  card.append(createElement("h2", { text: "Швейцарский этап" }));
  const settings = tournament.swiss_stage_prediction || {};
  if (tournament.is_archived === true) {
    if (!settings.is_enabled) {
      card.append(createElement("p", {
        className: "subtitle",
        text: "Прогноз не был включён.",
      }));
      return card;
    }
    const teamsById = new Map(
      tournament.teams.map((team) => [team.id, team.name]),
    );
    const directNames = (settings.direct_qualifier_team_ids || [])
      .map((teamId) => teamsById.get(teamId) || `#${teamId}`)
      .join(", ");
    const eliminationNames = (settings.elimination_qualifier_team_ids || [])
      .map((teamId) => teamsById.get(teamId) || `#${teamId}`)
      .join(", ");
    for (const line of [
      `Дедлайн: ${formatMatchStartsAt(settings.deadline_at)}`,
      `Прошли напрямую: ${directNames || "не указаны"}`,
      `Через стыковой раунд: ${eliminationNames || "не указаны"}`,
    ]) {
      card.append(createElement("p", { className: "subtitle", text: line }));
    }
    return card;
  }
  const form = createElement("form", { className: "form-fields" });
  const enabled = createElement("input");
  enabled.type = "checkbox";
  enabled.checked = settings.is_enabled === true;
  const deadline = createElement("input", { className: "text-input" });
  deadline.type = "datetime-local";
  deadline.value = settings.deadline_at
    ? formatDateTimeLocalValue(settings.deadline_at)
    : "";
  const directCount = createElement("input", { className: "text-input" });
  const eliminationCount = createElement("input", { className: "text-input" });
  for (const input of [directCount, eliminationCount]) {
    input.type = "number";
    input.min = "1";
  }
  directCount.value = String(settings.direct_qualifier_count ?? 3);
  eliminationCount.value = String(settings.elimination_qualifier_count ?? 5);
  const message = createElement("p", { className: "form-message" });
  const saveButton = createActionButton("Сохранить общие настройки", "primary-action-button", "submit");
  form.append(
    createLabeledField("Включить прогноз", enabled),
    createLabeledField("Дедлайн", deadline),
    createLabeledField("Прямые проходы", directCount),
    createLabeledField("Через стыковой раунд", eliminationCount),
    saveButton,
    message,
  );
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    saveButton.disabled = true;
    try {
      await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournament.id}/swiss-stage/settings`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          enabled: enabled.checked,
          deadline_at: enabled.checked && deadline.value
            ? new Date(deadline.value).toISOString()
            : null,
          direct_qualifier_count: Number(directCount.value),
          elimination_qualifier_count: Number(eliminationCount.value),
          expected_version: tournament.version,
        }),
      });
      void openSharedTournament(bootstrap, tournament.id, {
        message: "Настройки швейцарского этапа обновлены во всех чатах.",
        messageType: "success",
      });
    } catch (error) {
      setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
      saveButton.disabled = false;
    }
  });
  card.append(form);

  const deadlinePassed = settings.deadline_at
    && new Date(settings.deadline_at).getTime() <= Date.now();
  if (settings.is_enabled && deadlinePassed && tournament.teams.length) {
    const resultForm = createElement("form", { className: "form-fields" });
    const direct = document.createElement("select");
    const elimination = document.createElement("select");
    for (const select of [direct, elimination]) {
      select.className = "text-input";
      select.multiple = true;
      select.size = Math.min(10, Math.max(4, tournament.teams.length));
      for (const team of tournament.teams) {
        const option = document.createElement("option");
        option.value = String(team.id);
        option.textContent = team.name;
        select.append(option);
      }
    }
    const directIds = new Set(settings.direct_qualifier_team_ids || []);
    const eliminationIds = new Set(settings.elimination_qualifier_team_ids || []);
    for (const option of direct.options) option.selected = directIds.has(Number(option.value));
    for (const option of elimination.options) option.selected = eliminationIds.has(Number(option.value));
    const resultButton = createActionButton("Сохранить итоги во всех чатах", "secondary-action-button", "submit");
    resultForm.append(
      createLabeledField("Прошли напрямую", direct),
      createLabeledField("Прошли через стыковой раунд", elimination),
      resultButton,
    );
    resultForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      resultButton.disabled = true;
      try {
        await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournament.id}/swiss-stage/result`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            direct_team_ids: Array.from(direct.selectedOptions, (option) => Number(option.value)),
            elimination_team_ids: Array.from(elimination.selectedOptions, (option) => Number(option.value)),
            expected_version: tournament.version,
          }),
        });
        void openSharedTournament(bootstrap, tournament.id, {
          message: "Итоги швейцарского этапа и рейтинги обновлены во всех чатах.",
          messageType: "success",
        });
      } catch (error) {
        setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
        resultButton.disabled = false;
      }
    });
    card.append(resultForm);
  }
  return card;
}

function createSharedMatchCreationCard(bootstrap, tournament, state = {}) {
  const card = createElement("section", { className: "info-card" });
  const heading = createElement("h2", { text: "Добавить матч" });
  if (!Array.isArray(tournament.teams) || tournament.teams.length < 2) {
    card.append(
      heading,
      createElement("p", {
        className: "subtitle",
        text: "Сначала добавьте минимум две команды.",
      }),
    );
    return card;
  }
  const form = createElement("form", { className: "form-fields" });
  const homeSelect = document.createElement("select");
  const awaySelect = document.createElement("select");
  homeSelect.className = "text-input";
  awaySelect.className = "text-input";
  for (const team of tournament.teams) {
    for (const select of [homeSelect, awaySelect]) {
      const option = document.createElement("option");
      option.value = String(team.id);
      option.textContent = team.name;
      select.append(option);
    }
  }
  awaySelect.selectedIndex = 1;
  const startInput = createElement("input", { className: "text-input" });
  startInput.type = "datetime-local";
  startInput.required = true;
  const fields = [
    createLabeledField("Первая команда", homeSelect),
    createLabeledField("Вторая команда", awaySelect),
    createLabeledField("Начало и дедлайн", startInput),
  ];
  let bestOfSelect = null;
  if (tournament.template_key === "the_international_2026") {
    bestOfSelect = document.createElement("select");
    bestOfSelect.className = "text-input";
    for (const value of [3, 5]) {
      const option = document.createElement("option");
      option.value = String(value);
      option.textContent = `Bo${value}`;
      bestOfSelect.append(option);
    }
    fields.push(createLabeledField("Формат серии", bestOfSelect));
  }
  const message = createElement("p", { className: "form-message" });
  setFormMessage(message, state.matchMessage || "", state.matchMessageType || "");
  const submitButton = createActionButton(
    "Добавить матч",
    "primary-action-button",
    "submit",
  );
  form.append(...fields, submitButton, message);
  card.append(heading, form);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const startsAt = new Date(startInput.value);
    if (Number.isNaN(startsAt.getTime())) {
      setFormMessage(message, "Укажите дату и время начала.", "error");
      return;
    }
    submitButton.disabled = true;
    try {
      await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournament.id}/matches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          home_team_id: Number(homeSelect.value),
          away_team_id: Number(awaySelect.value),
          starts_at_utc: startsAt.toISOString(),
          best_of: bestOfSelect ? Number(bestOfSelect.value) : null,
        }),
      });
      void openSharedTournament(bootstrap, tournament.id, {
        matchMessage: "Матч добавлен во все активные связанные конкурсы.",
        matchMessageType: "success",
      });
    } catch (error) {
      setFormMessage(
        message,
        error instanceof Error ? error.message : "Не удалось добавить матч.",
        "error",
      );
      submitButton.disabled = false;
    }
  });
  return card;
}

function createLabeledField(labelText, input) {
  const field = createElement("label", { className: "form-field" });
  field.append(
    createElement("span", { className: "form-field-label", text: labelText }),
    input,
  );
  return field;
}

function createSharedMatchAdministrationCard(bootstrap, tournament, match) {
  const card = createElement("section", { className: "info-card" });
  const heading = createElement("h2", {
    text: `${match.home_team.name} — ${match.away_team.name}`,
  });
  const impact = createElement("p", {
    className: "subtitle",
    text: (
      `${formatMatchStartsAt(match.starts_at_utc)} · `
      + `${match.linked_contest_count} ${getRussianPlural(match.linked_contest_count, "конкурс", "конкурса", "конкурсов")} · `
      + `${match.prediction_count} ${getRussianPlural(match.prediction_count, "прогноз", "прогноза", "прогнозов")}`
    ),
  });
  const message = createElement("p", { className: "form-message" });
  card.append(heading, impact, message);

  if (tournament.is_archived === true) {
    const advancingTeam = [match.home_team, match.away_team].find(
      (team) => team.id === match.result?.advancing_team_id,
    );
    const resultText = match.result
      ? (
          `Итог: ${match.result.home_score}:${match.result.away_score}`
          + (advancingTeam ? ` · прошла дальше ${advancingTeam.name}` : "")
        )
      : `Статус: ${match.status}`;
    card.append(createElement("p", { className: "subtitle", text: resultText }));
    return card;
  }

  const deadlinePassed = new Date(match.starts_at_utc).getTime() <= Date.now();
  if (match.status === "scheduled" && !deadlinePassed) {
    const timeForm = createElement("form", { className: "form-fields" });
    const startInput = createElement("input", { className: "text-input" });
    startInput.type = "datetime-local";
    startInput.value = formatDateTimeLocalValue(match.starts_at_utc);
    const saveButton = createActionButton(
      "Изменить время во всех чатах",
      "secondary-action-button",
      "submit",
    );
    timeForm.append(createLabeledField("Начало и дедлайн", startInput), saveButton);
    timeForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const startsAt = new Date(startInput.value);
      saveButton.disabled = true;
      try {
        await apiRequestForCurrentView(
          `/api/tma/shared-tournaments/${tournament.id}/matches/${match.id}`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              starts_at_utc: startsAt.toISOString(),
              expected_version: match.version,
            }),
          },
        );
        void openSharedTournament(bootstrap, tournament.id, {
          message: "Время обновлено во всех связанных конкурсах.",
          messageType: "success",
        });
      } catch (error) {
        setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
        saveButton.disabled = false;
      }
    });
    card.append(timeForm);
  }

  if (deadlinePassed || match.status === "finished") {
    const resultForm = createElement("form", { className: "form-fields" });
    const homeScore = createElement("input", { className: "text-input" });
    const awayScore = createElement("input", { className: "text-input" });
    for (const input of [homeScore, awayScore]) {
      input.type = "number";
      input.min = "0";
      input.required = true;
    }
    homeScore.value = match.result?.home_score ?? "";
    awayScore.value = match.result?.away_score ?? "";
    const advancing = document.createElement("select");
    advancing.className = "text-input";
    for (const team of [match.home_team, match.away_team]) {
      const option = document.createElement("option");
      option.value = String(team.id);
      option.textContent = team.name;
      advancing.append(option);
    }
    if (match.result?.advancing_team_id) {
      advancing.value = String(match.result.advancing_team_id);
    }
    const saveResultButton = createActionButton(
      match.result ? "Исправить результат во всех чатах" : "Внести результат во все чаты",
      "primary-action-button",
      "submit",
    );
    resultForm.append(
      createLabeledField(match.home_team.name, homeScore),
      createLabeledField(match.away_team.name, awayScore),
      createLabeledField("Прошла дальше", advancing),
      saveResultButton,
    );
    resultForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      saveResultButton.disabled = true;
      try {
        await apiRequestForCurrentView(
          `/api/tma/shared-tournaments/${tournament.id}/matches/${match.id}/result`,
          {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              home_score: Number(homeScore.value),
              away_score: Number(awayScore.value),
              advancing_team_id: Number(advancing.value),
              expected_version: match.version,
            }),
          },
        );
        void openSharedTournament(bootstrap, tournament.id, {
          message: "Результат и рейтинги обновлены во всех чатах.",
          messageType: "success",
        });
      } catch (error) {
        setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
        saveResultButton.disabled = false;
      }
    });
    card.append(resultForm);
  }

  const deleteButton = createActionButton(
    "Удалить матч во всех чатах",
    "danger-action-button",
  );
  deleteButton.addEventListener("click", async () => {
    const warning = (
      `Матч используется в ${match.linked_contest_count} `
      + `${getRussianPlural(match.linked_contest_count, "конкурсе", "конкурсах", "конкурсах")}. `
      + `Будут удалены ${match.prediction_count} `
      + `${getRussianPlural(match.prediction_count, "прогноз", "прогноза", "прогнозов")} `
      + "и начисленные баллы. Продолжить?"
    );
    if (!window.confirm(warning)) {
      return;
    }
    deleteButton.disabled = true;
    try {
      const result = await apiRequestForCurrentView(
        `/api/tma/shared-tournaments/${tournament.id}/matches/${match.id}`
          + `?expected_version=${match.version}`,
        { method: "DELETE" },
      );
      void openSharedTournament(bootstrap, tournament.id, {
        message: (
          `Матч удалён из ${result.linked_contest_count} `
          + `${getRussianPlural(result.linked_contest_count, "конкурса", "конкурсов", "конкурсов")}; `
          + `удалено прогнозов: ${result.deleted_prediction_count}.`
        ),
        messageType: "success",
      });
    } catch (error) {
      setFormMessage(message, error instanceof Error ? error.message : "Ошибка.", "error");
      deleteButton.disabled = false;
    }
  });
  card.append(deleteButton);
  return card;
}

function renderSharedTournamentScreen(bootstrap, tournament, state = {}) {
  setChatSummary();
  const cards = [
    createAdministrativeHeader(bootstrap, {
      title: tournament.name,
      backLabel: "← К общим турнирам",
      onBack: () => {
        void openSharedTournamentManagement(bootstrap);
      },
      description: (
        `${tournament.linked_contest_count} `
        + `${getRussianPlural(tournament.linked_contest_count, "связанный конкурс", "связанных конкурса", "связанных конкурсов")}`
      ),
    }),
  ];
  if (state.message) {
    cards.push(createInfoCard("Готово", [state.message]));
  }
  cards.push(
    createSharedTournamentLifecycleCard(bootstrap, tournament),
    createSharedTournamentTeamsCard(bootstrap, tournament, state),
    createSharedChampionCard(bootstrap, tournament),
    createSharedSwissStageCard(bootstrap, tournament),
  );
  if (tournament.is_archived !== true) {
    cards.push(createSharedMatchCreationCard(bootstrap, tournament, state));
  }
  for (const match of tournament.matches || []) {
    cards.push(createSharedMatchAdministrationCard(bootstrap, tournament, match));
  }
  replaceAppContent(...cards);
}

async function openSharedTournament(bootstrap, tournamentId, state = {}) {
  const viewToken = replaceAppContent(
    createStatusCard("Открываем общий турнир", "Загружаем матчи…"),
  );
  try {
    const result = await apiRequestForCurrentView(`/api/tma/shared-tournaments/${tournamentId}`);
    if (!isCurrentView(viewToken)) {
      return;
    }
    renderSharedTournamentScreen(bootstrap, result.shared_tournament, state);
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
    if (handleManagementRequestError(error)) {
      return;
    }
    replaceAppContent(
      createAdministrativeHeader(bootstrap, {
        title: "Общий турнир",
        backLabel: "← К общим турнирам",
        onBack: () => {
          void openSharedTournamentManagement(bootstrap);
        },
      }),
      createInfoCard(
        "Не удалось открыть турнир",
        [error instanceof Error ? error.message : "Неизвестная ошибка."],
      ),
    );
  }
}

function renderManagementScreen(bootstrap, managementData, state = {}) {
  setChatSummary();
  const contests = Array.isArray(managementData.contests)
    ? managementData.contests
    : [];
  const capabilities = managementData.capabilities || {};
  const cards = [
    createManagementHeaderCard(bootstrap),
    createManagementContestListCard(contests, bootstrap, managementData),
  ];

  if (capabilities.can_manage_shared_tournaments === true) {
    cards.push(createSharedTournamentNavigationCard(bootstrap, managementData));
  }

  if (capabilities.can_manage_chat_settings === true) {
    cards.push(createChatSettingsCard(bootstrap, managementData, state));
  }

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
  replaceAppContent(...cards);
}

function handleManagementRequestError(
  error,
  message = "Права изменились. Недоступный экран управления закрыт.",
) {
  if (error instanceof StaleViewRequestError) {
    return true;
  }
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
  setChatSummary();
  const viewToken = replaceAppContent(
    createStatusCard(
      "Открываем управление",
      "Проверяем права и загружаем конкурсы текущего чата…",
    ),
  );
  try {
    const result = await apiRequestForCurrentView("/api/tma/management/contests");
    if (!isCurrentView(viewToken)) {
      return;
    }
    renderManagementScreen(bootstrap, result || {}, state);
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
    if (error?.status === 403) {
      const nextBootstrap = {
        ...bootstrap,
        access: {
          ...bootstrap.access,
          can_manage_contests: false,
        },
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
    replaceAppContent(
      createManagementHeaderCard(bootstrap),
      createInfoCard("Не удалось открыть управление", [message]),
    );
  }
}

async function openContestList(bootstrap, state = {}) {
  const viewToken = renderLoading();

  try {
    const refreshedBootstrap = await apiRequestForCurrentView("/api/tma/bootstrap");
    if (!isCurrentView(viewToken)) {
      return;
    }
    activeBootstrap = refreshedBootstrap;
    renderContestScreen(refreshedBootstrap, state);
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
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
  replaceAppContent(...cards);
}

function renderBootstrap(bootstrap) {
  activeBootstrap = bootstrap;
  renderContestScreen(bootstrap);
}

function renderError(message, { canRetry = false } = {}) {
  setChatSummary("Не удалось открыть конкурсы.");
  const card = createInfoCard("Не удалось открыть Клевер", [message]);

  if (canRetry) {
    const actions = createElement("div", { className: "form-actions" });
    const retryButton = createActionButton(
      "Попробовать снова",
      "primary-action-button",
    );
    retryButton.addEventListener("click", () => {
      void initialize();
    });
    actions.append(retryButton);
    card.append(actions);
  }

  replaceAppContent(card);
}

async function apiRequest(path, options = {}) {
  const initData = getTelegramInitData();

  if (!initData) {
    throw new Error(buildMissingInitDataMessage());
  }

  let response;
  try {
    response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: {
        "X-Telegram-Init-Data": initData,
        ...(options.headers || {}),
      },
    });
  } catch {
    throw new Error(
      "Не удалось связаться с сервером. "
      + "Проверьте соединение и повторите попытку.",
    );
  }
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

  if (message === buildMissingInitDataMessage()) {
    renderError(message);
    return;
  }

  if (message === "Telegram init data start_param is required.") {
    renderError(buildMissingChatContextMessage());
    return;
  }

  if (message === "TMA launch token is expired.") {
    renderError(buildExpiredLaunchTokenMessage());
    return;
  }

  if (message === "TMA launch link is no longer active.") {
    renderError(buildInactiveLaunchTokenMessage());
    return;
  }

  renderError(message, { canRetry: true });
}

async function initialize() {
  const viewToken = renderLoading();
  refreshTelegramWebApp()?.ready?.();

  try {
    const bootstrap = await apiRequestForCurrentView("/api/tma/bootstrap");
    if (!isCurrentView(viewToken)) {
      return;
    }
    renderBootstrap(bootstrap);
  } catch (error) {
    if (!isCurrentView(viewToken)) {
      return;
    }
    handleError(error);
  }
}

void initialize();
