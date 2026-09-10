# Исполняемые проверки автосохранения TMA

Установка и запуск из корня репозитория:

```sh
python -m pip install -r requirements.txt -r requirements-browser.txt
python -m playwright install chromium
python -m pytest tests/browser
```

Для WebKit установите `python -m playwright install webkit` и задайте
`TMA_TEST_BROWSER=webkit` перед запуском pytest. В Linux зависимости браузера
можно установить командой `python -m playwright install --with-deps chromium webkit`.

Если пакет Playwright не установлен, обычный `python -m pytest` явно пропускает
эти проверки. Если пакет установлен, но выбранного браузера нет, проверки падают
с командой установки. Node.js-зависимости и сборка frontend не требуются.

Браузер загружает неизменённые `tma/index.html`, `styles.css` и весь `app.js` через
настоящий FastAPI `TestClient`. Конкурсы, команды, матчи и прогнозы создаются
реальными API в отдельной временной SQLite для каждого теста. Проверяются как
интерфейс, так и сохранённые данные, прочитанные через API. Lifespan приложения
не запускается, поэтому polling, фоновые воркеры и внешние вызовы Telegram не
выполняются. Подменены только Telegram WebApp-контекст, ответ проверки
администраторов, часы в граничных сценариях и доставка отдельных HTTP-запросов.

`hold_next` удерживает запрос до `release` или `abort`; с `persist=True` сначала
выполняется реальный API, что воспроизводит потерю ответа после записи в БД.
`fail_next` обрывает одну доставку. Никакие функции приложения не извлекаются и
не переписываются; часы Playwright управляют настоящими обработчиками событий
и таймерами. События `pagehide`, `pageshow` и `visibilitychange` подаются явно:
это проверка обработчиков WebView, а не эмуляция завершения процесса Telegram.

При падении сохраняются снимок страницы и Playwright trace в
`tmp/browser-artifacts/<browser>/`. Открыть trace можно командой
`python -m playwright show-trace <путь-к-файлу.zip>`.
