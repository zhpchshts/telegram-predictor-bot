"use strict";

let telegram = window.Telegram?.WebApp || null;

const TELEGRAM_INIT_DATA_QUERY_PARAM = "tgWebAppData";
const IDEMPOTENCY_KEY_HEADER = "Idempotency-Key";
const CONTEST_NAME_MAX_LENGTH = 80;
const CONTEST_TABS = [
  { id: "predictions", label: "Прогнозы" },
  { id: "leaderboard", label: "Рейтинг" },
  { id: "matches", label: "Матчи" },
];

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
  chatSummaryElement.textContent = "Загружаем конкурсы этого чата…";
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
        "Создай первый конкурс прогнозов на Чемпионат мира 2026.",
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
        ? (
          "Открой конкурс, чтобы делать прогнозы, смотреть рейтинг "
          + "и управлять матчами."
        )
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
	  renderContestDetailsScreen(bootstrap, contest, {
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
    renderContestDetailsScreen(bootstrap, contest, {
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

      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: `Конкурс «${result.contest.name}» завершён.`,
        formMessageType: "success",
      });
    } catch (error) {
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось завершить конкурс.";

      renderContestDetailsScreen(bootstrap, contest, {
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
	  renderContestDetailsScreen(bootstrap, contest, {
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
    renderContestDetailsScreen(bootstrap, contest, {
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

      renderContestScreen(nextBootstrap, {
        mode: "form",
        formMessage: `Конкурс «${contest.name}» удалён.`,
        formMessageType: "success",
      });
    } catch (error) {
      const errorMessage = error instanceof Error
        ? error.message
        : "Не удалось удалить конкурс.";

      renderContestDetailsScreen(bootstrap, contest, {
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

function createLeaderboardCard(leaderboard, championPrediction) {
  const entries = Array.isArray(leaderboard) ? leaderboard : [];
  const championPredictionSlotsCount =
    championPrediction?.is_enabled === true ? 1 : 0;

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
    const totalMatchesCount = Number.isSafeInteger(
      entry?.total_matches_count,
    )
      ? entry.total_matches_count
      : 0;

    const item = createElement("li", {
      className: "leaderboard-list-item",
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
        `${totalMatchesCount + championPredictionSlotsCount}`
      ),
    });
    const pointsElement = createElement("span", {
      className: "leaderboard-points",
      text: `${totalPoints} ${getPointsLabel(totalPoints)}`,
    });

    participantElement.append(nameElement, predictionsElement);
    item.append(placeElement, participantElement, pointsElement);
    list.append(item);
  }

  card.append(heading, list);

  return card;
}

function createContestRulesCard(championPrediction) {
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

function createMatchResultSection(contest, match, state, onResultSaved) {
  const result = match.result;
  const section = createElement("div", {
    className: "match-result-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Результат матча",
  });

  if (contest.is_active === false) {
    const readOnlyMessage = createElement("p", {
      className: "match-prediction-closed",
      text: result
        ? (
          `Итоговый счёт: ${result.home_score} : ${result.away_score}. `
          + `Победитель противостояния: `
          + `${getTeamNameById(match, result.advancing_team_id)}.`
        )
        : "Конкурс завершён. Результаты доступны только для просмотра.",
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

function formatDateTimeLocalValue(utcValue) {
  if (typeof utcValue !== "string") {
    return "";
  }

  const date = new Date(utcValue);

  if (Number.isNaN(date.getTime())) {
    return "";
  }

  const year = String(date.getFullYear());
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");

  return `${year}-${month}-${day}T${hours}:${minutes}`;
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

  if (!championPrediction.is_tournament_completed) {
    section.append(
      createElement("p", {
        className: "match-prediction-closed",
        text: (
          "Фактического чемпиона можно указать после завершения " +
          "всех матчей конкурса."
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
    createChampionPredictionSettingsDisclosure(
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

function createMatchListItem(
  contest,
  match,
  state,
  onResultSaved,
  onMatchDeletionStateChange,
  { showPredictions, showResults },
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
  const sections = [header, meta];

  header.append(teams, status);
  meta.append(startsAt);

  if (showPredictions) {
    sections.push(createMatchPredictionSection(contest, match));
  }

  if (showResults) {
    sections.push(
      createMatchResultSection(contest, match, state, onResultSaved),
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

  if (championPrediction.is_enabled) {
    items.push({
      kind: "champion",
      isOpen: championPrediction.is_open,
      sortTime: getPredictionSortTime(championPrediction.deadline_at),
      sortKey: "champion",
    });
  }

  return items
    .sort(comparePredictionListItems)
    .map((item) => {
      if (item.kind === "champion") {
        return createChampionPredictionCard(contest, onChampionUpdated);
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
    text: "Любой участник этого чата может добавить матч в конкурс.",
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
  startsAtInput.value = draft.startsAtLocal || "";
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

      renderContestDetailsScreen(bootstrap, updatedContest, {
        activeTab: "matches",
        matchFormMessage: successMessage,
        matchFormMessageType: "success",
      });
    } catch (error) {
      const errorMessage =
        error instanceof Error ? error.message : "Не удалось добавить матч.";

      renderContestDetailsScreen(bootstrap, contest, {
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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;
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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;
  appContentElement.replaceChildren(card);
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
      createLeaderboardCard(leaderboard, contest.champion_prediction),
    );
  } else if (activeTab === "matches") {
    cards.push(
      createMatchesCard(
        contest,
        matches,
        state,
        (resultState) => {
          void openContest(bootstrap, contest.id, {
            ...state,
            activeTab: "matches",
            resultMatchId: resultState.matchId,
            resultMessage: resultState.message,
            resultMessageType: resultState.type,
          });
        },
        (deletionState) => {
          void openContest(bootstrap, contest.id, {
            ...state,
            activeTab: "matches",
            ...deletionState,
          });
        },
        {
          title: "Матчи",
          emptyMessages: isActive
            ? ["Матчей пока нет.", "Добавьте первый матч ниже."]
            : ["Матчей нет."],
          showPredictions: false,
          showResults: true,
          leadingItems: isActive
            ? [
              createChampionAdministrationCard(
                contest,
                state,
                () => {
                  void openContest(bootstrap, contest.id, {
                    ...state,
                    activeTab: "matches",
                  });
                },
              ),
            ]
            : [],
        },
      ),
    );

	if (isActive) {
	  cards.push(
		createMatchFormCard(bootstrap, contest, state),
		createContestCompletionCard(bootstrap, contest, state),
		createContestDeletionCard(bootstrap, contest, state),
	  );
	}
  } else {
    cards.push(
      createContestRulesCard(contest.champion_prediction),
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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;
  appContentElement.replaceChildren(...cards);
}

async function openContest(bootstrap, contestId, state = {}) {
  renderContestDetailsLoading(bootstrap);

  try {
    const result = await apiRequest(`/api/tma/contests/${contestId}`);

    if (!result || !result.contest) {
      throw new Error("Сервер вернул некорректный ответ при открытии конкурса.");
    }

    renderContestDetailsScreen(bootstrap, result.contest, state);
  } catch (error) {
    const errorMessage =
      error instanceof Error ? error.message : "Не удалось открыть конкурс.";

    renderContestDetailsError(bootstrap, errorMessage);
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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;

  const contestCard = createContestsCard(
    activeContests,
    completedContests,
    (contestId) => {
      void openContest(bootstrap, contestId);
    },
  );
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
