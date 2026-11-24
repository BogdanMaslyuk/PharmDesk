# PharmDesk Development Bridge v0.1

Development Bridge связывает утверждённый GitHub Issue с локальным Codex, но
не превращает появление Issue в разрешение на исполнение. Каждый вариант
задания сначала просматривается, получает SHA-256 и отдельно утверждается
владельцем. Комментарии Issue не используются как команды.

Bridge не является службой: он не создаёт автозапуск, не слушает порт и не
опрашивает GitHub в фоне. Каждая команда запускается пользователем вручную.

## Каталоги

- код: `PharmDesk-DevBridge\tools\dev_bridge`;
- конфигурация и состояние: `_DevBridge\config.json`, `_DevBridge\state`;
- локальные журналы: `_DevBridge\state\runs`;
- изолированные рабочие копии задач: `_DevBridge\workspaces`;
- локальный CLI: `_DevBridge\codex-cli`;
- отдельный Codex home: `_DevBridge\codex-home`.

Рабочая `PharmDesk-git`, историческая `_Validation_047`, P3-песочницы и
зарегистрированные приватные каталоги должны находиться в `protected_roots`.
Пути указываются абсолютно и без шаблонов.

## Модель безопасности

1. Разрешены только репозиторий `BogdanMaslyuk/PharmDesk` и реальный
   `author.login == BogdanMaslyuk`, полученный через GitHub API/CLI.
2. Digest включает заголовок, полный текст Issue, автора, время изменения и
   неизменяемые параметры запуска. Любое изменение после `approve` возвращает
   задачу в `proposed`.
3. Одобрение одноразовое. Повторный `run` требует нового `approve`.
4. Одновременно существует не более одного запуска. После аварии stale-lock
   снимается только явной командой `recover` и только если процесс уже завершён.
5. Текст Issue передаётся как недоверенный контент. Bridge не извлекает из него
   PowerShell, тестовые команды, пути, разрешения или параметры subprocess.
6. Workspace создаётся только под `_DevBridge\workspaces`. Traversal,
   junction/symlink/reparse point, SQLite, CSV/XLS/XLSX, приватные каталоги и
   крупные файлы приводят к безопасному отказу.
7. Для Codex используется отдельный профиль: чтение диска по умолчанию
   запрещено, workspace доступен на запись, сеть команд выключена, approvals
   остаются `on-request`, Windows sandbox фиксирован как MXC.
8. Реальный адаптер нельзя использовать без успешного `probe-isolation`,
   совпадающих SHA-256 конфигурации/версии CLI и отдельной аутентификации CLI.
   Ослабление до legacy sandbox не выполняется автоматически.
9. Статус `completed` возможен только при exit code 0 и реально пройденных
   тестах. Mock-адаптер всегда заканчивает как `needs_approval`.
10. `run` никогда не выполняет `git push`, не создаёт PR и не публикует отчёт.
    Публичный комментарий возможен только отдельной командой с digest уже
    отфильтрованного отчёта.

Публичный отчёт содержит только: Issue, run id, состояние, сгенерированную
ветку, безопасные относительные имена изменённых файлов, числа тестов, commit
(если он действительно создан) и коды ограничений. Полный prompt, консольный
журнал, абсолютные домашние пути и произвольный текст агента не публикуются.

## Первоначальная настройка

Требуются Git, GitHub CLI с действующей авторизацией, Python 3.14 и Node.js.
Системный `python.exe` не используется — команды ниже явно вызывают Python
Launcher.

Официальный Codex CLI устанавливается локально, без глобального PATH:

```powershell
& 'C:\Program Files\nodejs\npm.cmd' install `
  --prefix "$env:USERPROFILE\Desktop\PharmDesk_Project\_DevBridge\codex-cli" `
  --no-save --no-package-lock --no-audit --no-fund `
  '@openai/codex@0.172.0'
```

Скопируйте `config.example.json` в `_DevBridge\config.json`, замените
`YOUR_USER`, зарегистрируйте точные защищённые пути и сначала оставьте
`"runner_adapter": "mock"`. Секретов в конфигурации быть не должно.

Проверки:

```powershell
cd "$env:USERPROFILE\Desktop\PharmDesk_Project\PharmDesk-DevBridge"
tools\dev_bridge\bridge.cmd doctor
tools\dev_bridge\bridge.cmd probe-isolation
```

`probe-isolation` не вызывает модель. Он запускает фиксированную команду в
нативной песочнице и требует одновременно подтвердить запись внутри учебного
workspace и запрет чтения/записи синтетического canary снаружи. Любая ошибка
оставляет адаптер в mock-режиме.

## Обычный цикл задачи

```powershell
# 1. Получить проверенный автор/текст и digest, но ничего не исполнять
tools\dev_bridge\bridge.cmd preview 123

# 2. После проверки человеком разрешить только показанный вариант
tools\dev_bridge\bridge.cmd approve 123 <SHA256_ИЗ_PREVIEW>

# 3. Однократный запуск
tools\dev_bridge\bridge.cmd run 123

# 4. Просмотр состояния
tools\dev_bridge\bridge.cmd status 123

# 5. Просмотр обезличенного отчёта и его отдельного digest
tools\dev_bridge\bridge.cmd report-preview 123
```

Команда ниже является отдельной внешней публикацией. Её нельзя выполнять без
самостоятельного разрешения владельца на конкретный digest:

```powershell
tools\dev_bridge\bridge.cmd publish-report 123 <REPORT_SHA256>
```

Bridge добавит только безопасный структурированный комментарий в Issue. Он не
создаёт нового сообщения в текущем разговоре ChatGPT: ChatGPT должен повторно
прочитать Issue через GitHub.

## Остановка и восстановление

```powershell
tools\dev_bridge\bridge.cmd stop
tools\dev_bridge\bridge.cmd status
tools\dev_bridge\bridge.cmd recover
```

`stop` создаёт локальный запрос остановки. `recover` не прерывает живой процесс
и снимает только доказанно stale-lock, переводя незавершённую задачу в
`cancelled`. Workspaces и журналы автоматически не удаляются.

Чтобы отключить Bridge, достаточно не запускать команды. Фонового процесса,
службы или задания планировщика нет.

## Тесты

Синтетические тесты не используют SQLite PharmDesk:

```powershell
py -V:3.14 -m unittest discover -s tools\dev_bridge\tests -v
```

Полная регрессия PharmDesk запускается существующим проектным окружением:

```powershell
& '<путь-к-существующей-.venv>\Scripts\python.exe' `
  -m pytest -q -p no:cacheprovider --basetemp '<новый-каталог-в-C:\tmp>' tests
```

## Известные ограничения v0.1

- Подтверждение выполняется локально; одного факта появления Issue недостаточно.
- Для запуска настоящего Codex необходима нативная песочница, доступная на
  конкретном Windows executor, и отдельная авторизация изолированного CLI.
- Bridge не делает commit/push/PR. Эти действия остаются отдельными этапами с
  самостоятельным разрешением пользователя.
- Свободный полный журнал хранится только локально и не считается безопасным
  для публикации.
- В текущей проверенной установке MXC недоступен, поэтому разрешён только
  mock-адаптер до отдельной настройки Windows и повторного успешного probe.
