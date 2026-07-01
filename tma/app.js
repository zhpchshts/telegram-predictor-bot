"use strict";

const statusElement = document.querySelector("#app-status");
const telegram = window.Telegram?.WebApp;

if (telegram) {
  telegram.ready();
  telegram.expand();

  document.documentElement.style.setProperty(
    "--tg-viewport-height",
    telegram.viewportHeight + "px",
  );

  telegram.onEvent("viewportChanged", () => {
    document.documentElement.style.setProperty(
      "--tg-viewport-height",
      telegram.viewportHeight + "px",
    );
  });

  if (telegram.initDataUnsafe?.user) {
    const { first_name: firstName } = telegram.initDataUnsafe.user;

    statusElement.textContent =
      `Привет, ${firstName}. Скоро здесь появятся матчи и прогнозы конкурса.`;
  }
} else {
  statusElement.textContent =
    "Страница открыта в браузере. В Telegram здесь будет доступен конкурс прогнозов.";
}