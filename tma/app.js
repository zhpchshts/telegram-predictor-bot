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

function createContestsCard(contests, onOpenContest) {
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
    text: "Выбери конкурс, чтобы добавить матчи и настроить его дальше.",
  });
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

function createContestDetailsCard(contest, onBack) {
  const card = createElement("section", {
    className: "info-card contest-details-card",
  });
  const status = createElement("p", {
    className: "contest-status",
    text: "Активный конкурс",
  });
  const heading = createElement("h2", {
    text: contest.name,
  });
  const description = createElement("p", {
    className: "subtitle",
    text: "Добавляйте матчи, чтобы участники могли делать прогнозы.",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  const backButton = createActionButton(
    "К списку конкурсов",
    "secondary-action-button",
  );

  backButton.addEventListener("click", onBack);

  actions.append(backButton);
  card.append(status, heading, description, actions);
  return card;
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
      hint.textContent = "Сначала укажите итоговый счёт матча.";
    } else if (scoreState.isDraw) {
      field.disabled = false;

      if (resetDrawSelection && !previousScoreState.isDraw) {
        homeRadio.checked = false;
        awayRadio.checked = false;
      }

      hint.textContent =
        "При ничьей выберите команду, победившую в серии пенальти.";
    } else {
      const advancingTeamId =
        scoreState.homeScore > scoreState.awayScore
          ? match.home_team_id
          : match.away_team_id;

      homeRadio.checked = advancingTeamId === match.home_team_id;
      awayRadio.checked = advancingTeamId === match.away_team_id;
      field.disabled = true;
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

function createMatchResultSection(contest, match, state, onResultSaved) {
  const result = match.result;
  const section = createElement("div", {
    className: "match-prediction-section",
  });
  const heading = createElement("h3", {
    className: "match-prediction-heading",
    text: "Результат матча",
  });

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
  section.append(heading, hint, form);

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

  if (!isMatchPredictionOpen(match)) {
    const closedMessage = createElement("p", {
      className: "match-prediction-closed",
      text: prediction
        ? (
          `Ваш прогноз: ${prediction.home_score} : ` +
          `${prediction.away_score}. Победитель противостояния: ` +
          `${getTeamNameById(match, prediction.advancing_team_id)}.`
        )
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
      "Укажите итоговый счёт после 90 или 120 минут. " +
      "При ничьей выберите победителя серии пенальти. " +
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
  const message = createElement("p", {
    className: "form-message",
  });
  const actions = createElement("div", {
    className: "form-actions",
  });
  let submitLabel = prediction
    ? "Сохранить изменения"
    : "Сохранить прогноз";
  const submitButton = createActionButton(
    submitLabel,
    "primary-action-button",
    "submit",
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
    selectedAdvancingTeamId: prediction
      ? prediction.advancing_team_id
      : null,
  });

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
  section.append(heading, hint, form);

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
      const result = await apiRequest(
        `/api/tma/contests/${contest.id}/matches/${match.id}/prediction`,
        {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            predicted_home_score: homeScore,
            predicted_away_score: awayScore,
            predicted_advancing_team_id: advancingTeamId,
          }),
        },
      );

      if (!result || !result.prediction) {
        throw new Error(
          "Сервер вернул некорректный ответ при сохранении прогноза.",
        );
      }

      match.prediction = result.prediction;
      homeScoreInput.value = String(result.prediction.home_score);
      awayScoreInput.value = String(result.prediction.away_score);
      submitLabel = "Сохранить изменения";
      submitButton.textContent = submitLabel;
      setFormMessage(
        message,
        result.was_created
          ? "Прогноз сохранён."
          : "Прогноз обновлён.",
        "success",
      );
    } catch (error) {
      const errorMessage =
        error instanceof Error
          ? error.message
          : "Не удалось сохранить прогноз.";

      setFormMessage(message, errorMessage, "error");
      submitButton.textContent = submitLabel;
    } finally {
      submitButton.disabled = false;
    }
  });

  return section;
}

function createMatchesCard(contest, matches, state, onResultSaved) {
  if (matches.length === 0) {
    return createInfoCard(
      "Матчи",
      [
        "Матчей пока нет.",
        "Добавьте первый матч ниже. После этого участники смогут перейти к прогнозам.",
      ],
      "matches-card",
    );
  }

  const card = createElement("section", {
    className: "info-card matches-card",
  });
  const heading = createElement("h2", {
    text: "Матчи",
  });
  const list = createElement("ol", {
    className: "match-list",
  });

  for (const match of matches) {
    const item = createElement("li", {
      className: "match-list-item",
    });
    const teams = createElement("strong", {
      className: "match-teams",
      text: `${match.home_team_name} — ${match.away_team_name}`,
    });
    const startsAt = createElement("p", {
      className: "match-meta",
      text: `Начало: ${formatMatchStartsAt(match.starts_at_utc)}`,
    });
    const status = createElement("p", {
      className: "match-status",
      text: getMatchStatusLabel(match.status),
    });

    item.append(
      teams,
      startsAt,
      status,
      createMatchPredictionSection(contest, match),
      createMatchResultSection(contest, match, state, onResultSaved),
    );
    list.append(item);
  }

  card.append(heading, list);
  return card;
}

function createMatchFormCard(bootstrap, contest, state) {
  const draft = state.matchDraft || {};
  const card = createElement("section", {
    className: "info-card contest-form-card",
  });
  const heading = createElement("h2", {
    text: "Добавить матч",
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
  card.append(heading, description, form);

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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;
  appContentElement.replaceChildren(
    createContestDetailsCard(contest, () => {
      renderContestScreen(bootstrap);
    }),
    createMatchesCard(contest, matches, state, (resultState) => {
      void openContest(bootstrap, contest.id, {
        ...state,
        resultMatchId: resultState.matchId,
        resultMessage: resultState.message,
        resultMessageType: resultState.type,
      });
    }),
    createMatchFormCard(bootstrap, contest, state),
  );
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

  chatSummaryElement.textContent = `Привет, ${userName}. Чат «${chatTitle}».`;

  const contestCard = createContestsCard(activeContests, (contestId) => {
    void openContest(bootstrap, contestId);
  });
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