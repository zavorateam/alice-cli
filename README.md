# Alice CLI & API Gateway

Неофициальный клиент Яндекс Алисы с тремя режимами: интерактивный CLI, MCP stdio-сервер и OpenAI-совместимый HTTP gateway. Работает напрямую с внутренним WebSocket/RPC-протоколом Алисы (`wss://uniproxy.alice.yandex.ru/uni.ws`), используя OAuth-токен и/или cookies пользователя.

## Требования

**Python 3.9+** (используется `str.removeprefix` и `asyncio.to_thread`).

Установка:

```bash
git clone https://github.com/zavorateam/alice-cli.git
cd alice-cli
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Опционально для скачивания файлов Expert через браузер (Playwright):

```bash
pip install -r requirements-optional.txt
playwright install chromium
```

Без Playwright fallback для `get_source_file` не работает; в логах будет предупреждение.

## Авторизация

Поддерживаются два независимых источника:

**OAuth-токен** (используется для WebSocket/RPC). Начинается с `y0_`.
- `python alice_cli.py --token y0_...`
- `python alice_cli.py --token ./token.txt` (файл, первая строка — токен, вторая может содержать `device_uuid=...`)
- `python alice_cli.py --auth y0_...` (универсальный)
- `export ALICE_TOKEN=y0_...` или `export YANDEX_TOKEN=y0_...`

**Cookies** (нужны для safeclick, `get_source_file`, скачивания файлов).
- `python alice_cli.py --cookies .alice_cookies`
- `export ALICE_COOKIES=./cookies.txt`
- Формат: Netscape (из `Get cookies.txt LOCALLY`) или строка `name=value; name2=value2`.

**Оба одновременно** (рекомендуется для полного покрытия):
```bash
python alice_cli.py --token y0_... --cookies .alice_cookies
python alice_server.py --http --token y0_... --cookies .alice_cookies
```

Порядок поиска при отсутствии явных флагов:
`ALICE_TOKEN` → `YANDEX_TOKEN` → `ALICE_COOKIES` → `token.txt` → `.alice_token` → `.alice_cookies` → `cookies.txt`.

Перед стартом выполняется healthcheck через `POST /gproxy/get_alice_apps`. HTTP 200 — credentials приняты; 401/403 — отклонены; 413 — Cookie header слишком длинный. `Session_id` в cookies должен принадлежать тому же аккаунту, что OAuth-токен, иначе диалоги создаются «под другим устройством» и не видны в веб-интерфейсе.

**Не коммитьте** `.alice_cookies`, `token.txt`, `.alice_token`, `cookies.txt`, `.alice_device_uuid`. Все они в `.gitignore`.

Устройство определяется `.alice_device_uuid` (создаётся при первом запуске с правами `0600`); из cookies `alice_uuid`/`yandexuid` имеет приоритет.

## CLI

```bash
python alice_cli.py
python alice_cli.py --agent lavka -e -v
python alice_cli.py --token y0_... --cookies .alice_cookies --thinking
```

Параметры:

```
--auth AUTH           Токен, путь к токену или cookies
--token TOKEN         OAuth-токен или путь к файлу
--cookies PATH        Путь к cookies
--agent AGENT         Начальный агент (по умолчанию pro)
-e, --ephemeral       Временная сессия (диалоги удаляются при выходе)
-v, --verbose         Лог WS/RPC
--thinking            Показывать структурированный процесс размышлений
--tools-config PATH   JSON-конфиг локальных инструментов (по умолчанию alice_tools.json)
--no-tools            Отключить локальные инструменты
--no-meeseeks         Отключить Meeseeks
--yolo                Авто-одобрение всех вызовов инструментов (ПО УМОЛЧАНИЮ ВЫКЛ)
--dialog-id UUID      Продолжить существующий диалог
```

Команды в CLI (с `/` или без):

```
help, ?                 Справка
list, agents            Список агентов
agent <id>              Переключить агента
model <id>              То же
expert                  Переключить на Expert
new                     Новый диалог
dialogs                 Список диалогов (с индексами [1], [2], ...)
switch                  Текущий диалог
switch <uuid|N>         Переключиться на диалог по UUID или индексу
ephemeral on|off        Временный режим
clear                   Очистка экрана
tools                   Список загруженных инструментов
exec <cmd>              Shell-команда напрямую (обходит approval)
attach <path>           Прикрепить файл к следующему сообщению
meeseeks [@agent] <t>   Один изолированный саб-агент
meeseeks-batch <t1> | <t2>   Параллельные Meeseeks
yolo on|off             Авто-одобрение
exit, bye, quit, q      Выход
```

После ответа с вариантами (`[1]`, `[2]`, ...) ввод числа отправляет выбранный payload как `suggested_input`.

## Встроенные агенты

- `pro` — Pro (полные ответы, tool use, для Plus)
- `base` — Base (быстрый, повседневные вопросы)
- `expert` — Expert с длинными рассуждениями, trial ~5 запросов/24ч
- `deep_research` — автономный веб-поиск и синтез
- `best_price` — поиск лучших цен
- `legal` — юридические консультации со ссылками на законы РФ
- `taxi` / `lavka` / `market` / `gas` — Яндекс-приложения

Динамические **Alice Apps** дополнительно регистрируются из RPC `get_alice_apps` и отображаются в CLI по UUID и описанию.

## Локальные инструменты

Файл `alice_tools.json` (пример — `alice_tools.example.json`):

```json
{
  "shell": {
    "enabled": true,
    "strict": false,
    "blocked_commands": ["rm", "sudo", "dd", "systemctl", ...],
    "working_directory": "/tmp",
    "timeout_sec": 60
  },
  "files": {
    "root": "~",
    "write_root": "~/Downloads",
    "read_enabled": true,
    "write_enabled": true,
    "attach_enabled": true,
    "attach_max_bytes": 20971520,
    "write_max_bytes": 10485760
  },
  "meeseeks": {
    "enabled": true,
    "max_depth": 3,
    "max_concurrent": 5
  },
  "mcpServers": {
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}
  }
}
```

- `shell.strict=false` — разрешает пайпы и `&&`; blocked_commands проверяется по первому токену каждой команды.
- `files.root` — jail для чтения/списка/вложения.
- `files.write_root` — jail для `write_file`.
- Каждый вызов инструмента требует подтверждения (`y/N`), кроме случаев с `--yolo` или `yolo on`.
- `MCP stdio`-серверы из `mcpServers` стартуют как подпроцессы и регистрируются как `mcp__<server>__<tool>`.

## HTTP API

Запуск:

```bash
python alice_server.py --http --port 8000
# с предварительной авторизацией:
python alice_server.py --http --port 8000 --token y0_... --cookies .alice_cookies
```

По умолчанию слушает `127.0.0.1:8000`. Для доступа из сети — reverse-proxy с собственной аутентификацией; OAuth-токен Яндекса клиенту не передаётся.

### `POST /chat` (совместимость)

Тело — обычный UTF-8 текст. Авторизация через `Authorization` или `X-Alice-Auth`:

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Authorization: y0_...' \
  --data 'Привет, Алиса!'
```

Ответ — `text/plain`. При передаче `Authorization` создаётся временный `AliceAgentManager`, гарантированно закрывается в `finally`.

### `GET /v1/models`

OpenAI-совместимый каталог. Возвращает **только** модели в статусе `available` или `degraded`. Модели `implemented`, но ещё не проверенные, не публикуются.

```json
{
  "object": "list",
  "data": [
    {"id": "alice-base", "object": "model", "owned_by": "alice-cli",
     "alice": {"agent_id": "base", "requested_mode": "Base",
               "effective_mode": "Base", "status": "available"}}
  ]
}
```

### `GET /v1/models/diagnostics`

Полный инвентарь: `status` (`implemented` / `unknown` / `available` / `degraded` / `unavailable`), `effective_mode`, `effective_preset`, `reason`, `last_checked`, `elapsed_ms`, `trial_left`, `trial_limit`.

### `POST /v1/chat/completions`

Стандартный OpenAI-запрос. Поддерживает `stream`, `tools`, `tool_choice` (`auto` / `none`), `X-Alice-Session-ID`.

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Alice-Session-ID: my-session-1' \
  -d '{"model": "alice-base",
       "messages": [{"role":"user","content":"Привет!"}],
       "stream": true}'
```

- **Non-stream** → `chat.completion` с `finish_reason: stop` или `tool_calls`.
- **Stream** → `chat.completion.chunk` с дельтами `content`, финальный `finish_reason` и `data: [DONE]`. Если передан `tools` и Алиса вернула `<tool_call>`, контент буферизуется, выдаётся `delta.tool_calls` + `finish_reason: "tool_calls"`. Сервер **не исполняет** инструменты; клиент (OpenCode, DeepSeek Harness) сам выполняет и возвращает результат в `role: "tool"`.
- Без `X-Alice-Session-ID` каждый запрос создаёт изолированную сессию с uuid4-ключом — управляемой истории нет. Для диалоговой истории передавайте стабильный `X-Alice-Session-ID`.
- `model` должен быть из `/v1/models`. Недоступная модель → 503; неизвестная → 404.
- При ошибке после старта SSE возвращается `finish_reason: "stop"` и `[DONE]` с пустым контентом — клиент увидит пустой ответ, а не висящий стрим. Секреты и внутренние заголовки в тело не попадают.
- **Модели Pro/Expert не выдают себя за Pro при серверной деградации**: если Алиса ответила эффективным `Base`, статус модели становится `degraded` (не `available`); `/v1/models` такую модель публикует, но `/v1/models/diagnostics` показывает `effective_mode: "Base"` и `reason: "Pro -> Base"`.

## MCP stdio

```bash
python alice_server.py                       # без credentials — dynamic mode
python alice_server.py --token y0_...        # с credentials
```

В stdout — только JSON-RPC, в stderr — логи. `initialize`, `notifications/initialized` (без ответа по спецификации), `tools/list`, `tools/call`, `ping`, `-32601` для неизвестных методов.

Инструменты:
- `alice_ask_<agent_id>` — `message` (str, обязательный), `dialog_id` (str), `auth` (str). `auth` — OAuth или путь к файлу cookies; сервер создаёт временный `AliceAgentManager` под этот auth.
- `alice_click_action` — `action_text` (str), `dialog_id`, `auth`.

Пример `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "alice": {
      "command": "python3",
      "args": ["/path/to/alice-cli/alice_server.py"],
      "env": {"ALICE_TOKEN": "y0_..."}
    }
  }
}
```

## Архитектура

```
alice_auth.py                 credentials, cookies/token parsing, atomic write 0600, healthcheck
alice_server.py               lifecycle, create_curl_http_app, main_async, main
alice_cli.py                  интерактивный CLI (все команды)
tool_bridge.py, expert_bridge.py, expert_browser.py, meeseeks.py, local_tools.py

agents/                       встроенные агенты + registry (auto_discover по требованию)

tools/
  constants.py                константы протокола, uuid7
  parser.py                   _parse_nsb_card, compute_delta, parse_rich_uicard
  effective.py                normalize_effective_mode, EffectiveMode
  transport.py                AliceRpcClient, AliceWsEngine (listener, supervisor, delta, upload)
  manager.py                  GenericAppAgent, AliceAgentManager, ChatSession,
                              SessionRegistry, AliceMcpServer
  gateway.py                  ModelCatalog, /v1/models, /v1/models/diagnostics,
                              /v1/chat/completions, _flatten_messages, _extract_tool_calls
```

`tools/*` **не импортирует** `alice_server`; `alice_server` реэкспортирует публичные классы для обратной совместимости. Циклов нет.

Протокол:

- WebSocket: `wss://uniproxy.alice.yandex.ru/uni.ws` (SynchronizeState → ClientSubscriptionState → TextInput → VinsResponse / DeferredAliceResponse).
- RPC: `https://rpc.alice.yandex.ru` (`/gproxy/get_alice_apps`, `/dialog/list`, `/dialog/read_dialog`, `/dialog/remove_dialog`).

`DeferredAliceResponse` нормализуется в `AliceWsEngine._listen_loop` через `compute_delta`: накопительные снимки, настоящие дельты, идентичные кадры и регресс длины обрабатываются одинаково; потребители (CLI, SSE, MCP) получают накопительный текст в `AliceTurnResponse.text`.

`VinsResponse` нормализуется в `_handle_vins_response` через `normalize_effective_mode`: приоритет форм — `payload.effective_alice_2_settings` → `payload.response.alice_2_effective_settings` → `payload.alice_2_effective_settings` → `payload.effective_settings`. `trial_state` читается и обновляет `expert_trial_left/limit`. Отсутствие данных не превращается в «Base по умолчанию»: `effective_mode = None`, `degraded = False`.

## Тесты

```bash
pip install -r requirements-dev.txt   # pytest, httpx2
pytest -q tests/
```

Требуется `httpx2>=2.0` для `fastapi.testclient` на Starlette 1.x (см. `requirements-dev.txt`). Тесты не требуют credentials и работают на фикстурах из `tests/fixtures/`:

- `vins_*.json` — реальные формы `VinsResponse` (top-only, nested-only, оба, legacy, no-effective);
- `deferred_real_snapshot.json` — 5 накопительных снимков;
- `deferred_true_delta.json` — синтетические настоящие дельты.

MCP stdio проверяется запуском реального subprocess. Live-тесты против настоящей Алисы не входят в обычный набор — их нужно запускать вручную, с реальными credentials.

## Известные ограничения

- **Pro → Base**: сервер Алисы может деградировать Pro в Base. Это фиксируется как `degraded`, а не как подтверждённый Pro.
- **Expert trial ~5 запросов / 24 часа**. `/v1/models` не запускает healthcheck Expert чаще одного раза в сутки, чтобы не сжигать квоту.
- **Cookies нужны для safeclick и `get_source_file`** — операции «нажать кнопку в карточке» и «скачать файл» часто не работают только с OAuth.
- **WebSocket-протокол внутренний**, может измениться на стороне Яндекса без предупреждения.
- **HF MCP не настроен** в `alice_tools.example.json` — только `fetch`. Для подключения Hugging Face MCP добавьте в `mcpServers` собственный `uvx`-сервер.
- **`asyncio` как PyPI-пакет** — заглушка, не ставится; в `requirements.txt` его нет, потому что это stdlib.

## Лицензия

См. `LICENSE`. Проект не является официальным клиентом Яндекса. Пользователь самостоятельно проверяет применимые условия использования сервисов Яндекса.
