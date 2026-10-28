# Alice CLI

Неофициальный клиент и API-gateway для Яндекс Алисы. Три режима работы:

- **Интерактивный CLI** — чат с поддержкой локальных инструментов, ExpertBridge, Meeseeks, вложений.
- **MCP stdio-сервер** — интеграция с Claude Desktop, Cursor, Continue.
- **HTTP gateway** — OpenAI-совместимый API (`/v1/models`, `/v1/chat/completions`, `/v1/responses`), совместим с официальным `openai` Python SDK.

Работает напрямую с внутренним WebSocket/RPC-протоколом Алисы (`wss://uniproxy.alice.yandex.ru/uni.ws`), используя OAuth-токен и/или cookies пользователя.

## Требования

**Python 3.9+** (нужны `str.removeprefix`, `asyncio.to_thread`).

```bash
git clone https://github.com/zavorateam/alice-cli.git
cd alice-cli
python3 -m venv .venv
source .venv/bin/activate

# runtime
pip install -r requirements.txt

# dev/tests (pytest, httpx2, openai SDK)
pip install -r requirements-dev.txt

# опционально: Playwright fallback для скачивания файлов Expert
pip install -r requirements-optional.txt
playwright install chromium
```

Без Playwright HTTP-скачивание `get_source_file` работает, но браузерный fallback для случаев, когда сервер требует UI-клик, недоступен.

## Авторизация

Два независимых источника:

**OAuth-токен** (`y0_...`) — для WebSocket/RPC.
```bash
python alice_cli.py --token y0_...
python alice_cli.py --token ./token.txt      # файл: строка 1 = токен, строка 2 может содержать device_uuid=...
python alice_cli.py --auth y0_...            # универсальный
export ALICE_TOKEN=y0_...                    # или YANDEX_TOKEN
```

**Cookies** — для safeclick, `get_source_file`, скачивания файлов.
```bash
python alice_cli.py --cookies .alice_cookies
export ALICE_COOKIES=./cookies.txt
```
Поддерживаются Netscape (из «Get cookies.txt LOCALLY») и строки `name=value; name2=value2`.

**Оба одновременно** (рекомендуется):
```bash
python alice_cli.py --token y0_... --cookies .alice_cookies
python alice_server.py --http --token y0_... --cookies .alice_cookies
```

Порядок поиска при отсутствии флагов: `ALICE_TOKEN` → `YANDEX_TOKEN` → `ALICE_COOKIES` → `token.txt` → `.alice_token` → `.alice_cookies` → `cookies.txt`.

**Важно:** при явно переданных, но невалидных `--token`/`--cookies` fallback на env/файлы отключён (`strict_explicit=True`). Ошибка видна сразу, а не через скрытую подмену.

Healthcheck перед стартом: `POST /gproxy/get_alice_apps` с реальными заголовками. `200` — приняты, `401/403` — отклонены, `413` — Cookie header слишком длинный.

**Проверьте, что `.alice_cookies`, `token.txt`, `.alice_token`, `cookies.txt`, `.alice_device_uuid` в `.gitignore`.** Все они там по умолчанию; `.alice_device_uuid` создаётся с правами `0600`, запись атомарная.

`Session_id` в cookies должен принадлежать тому же аккаунту, что OAuth-токен, иначе диалоги создаются под другим устройством и не видны в веб-интерфейсе.

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
-e, --ephemeral       Временная сессия
-v, --verbose         INFO-логи WS/RPC в stderr
--thinking            Показывать структурированный процесс размышлений
--tools-config PATH   JSON-конфиг инструментов (по умолчанию alice_tools.json)
--no-tools            Отключить инструменты
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
dialogs                 Список диалогов с индексами [1], [2], ...
switch                  Текущий диалог
switch <uuid|N>         Переключиться по UUID или индексу
ephemeral on|off        Временный режим
clear                   Очистка экрана
tools                   Список загруженных инструментов
exec <cmd>              Shell-команда напрямую (обходит approval)
attach <path>           Прикрепить файл к следующему сообщению
meeseeks [@agent] <t>   Один изолированный саб-агент
meeseeks-batch <t1> | <t2>  Параллельные Meeseeks
yolo on|off             Авто-одобрение
exit, bye, quit, q      Выход
```

## Встроенные агенты

- `pro` — Pro (полные ответы, tool use, Plus)
- `base` — Base (быстрый, повседневный)
- `expert` — Expert с длинными рассуждениями, trial ~5/24ч
- `deep_research` — автономный веб-поиск
- `best_price` — поиск лучших цен
- `legal` — юридические консультации
- `taxi` / `lavka` / `market` / `gas` — Яндекс-приложения

Динамические Alice Apps добавляются из RPC `get_alice_apps`, отображаются в CLI по UUID и описанию.

## Локальные инструменты

Конфиг `alice_tools.json` (пример — `alice_tools.example.json`):

```json
{
  "shell": {
    "enabled": true,
    "strict": false,
    "blocked_commands": ["rm", "sudo", "dd", "systemctl"],
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
  "meeseeks": {"enabled": true, "max_depth": 3, "max_concurrent": 5},
  "mcpServers": {
    "fetch": {"command": "uvx", "args": ["mcp-server-fetch"]}
  }
}
```

- `files.root` по умолчанию `"~"`. **Не** `"/"` — вся ФС не открывается случайно. Если нужен `/`, задайте явно (будет `warning` в логе).
- `files.write_root` — jail для `write_file` (по умолчанию `~/Downloads`).
- `shell.strict=false` — пайпы и `&&`; `blocked_commands` по первым токенам — **не security boundary**. Warning в логе.
- Каждый вызов инструмента требует `y/N`, кроме `--yolo`/`yolo on`. `--yolo` по умолчанию **выключен**.

## MCP stdio

```bash
python alice_server.py                       # dynamic mode, без creds
python alice_server.py --token y0_... --cookies .alice_cookies
```

stdout — только JSON-RPC, stderr — логи. `initialize`, `notifications/initialized` (без ответа), `tools/list`, `tools/call`, `ping`, `-32601` для неизвестных методов.

Инструменты:
- `alice_ask_<agent_id>` — `message` (str), `dialog_id` (str), `auth` (str). `auth` — OAuth или путь к cookies; сервер создаёт временный `AliceAgentManager` с TTL/MAX-кэшем.
- `alice_click_action` — `action_text`, `dialog_id`, `auth`.

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

При EOF в `run_stdio` все кэшированные `AliceAgentManager` (и их WS-соединения, ephemeral-диалоги) закрываются через `_shutdown_all()`.

## HTTP gateway

```bash
python alice_server.py --http --port 8000
python alice_server.py --http --port 8000 --token y0_... --cookies .alice_cookies
```

По умолчанию `127.0.0.1:8000`. Для внешнего bind используйте reverse-proxy с собственной аутентификацией; OAuth Яндекса клиенту не передаётся.

### `POST /chat` (legacy, text/plain)

```bash
curl -X POST http://127.0.0.1:8000/chat \
  -H 'Authorization: y0_...' \
  --data 'Привет, Алиса!'
```
При `Authorization` создаётся временный manager, закрывается в `finally`.

### `GET /v1/models`

OpenAI-совместимый каталог. Возвращает **только** `available` и `degraded` модели.

### `GET /v1/models/{id}`

Детали одной модели, включая `implemented`/`unknown` (для отладки). В `/v1/models` (список) такие не публикуются.

### `GET /v1/models/diagnostics`

Полный инвентарь: `status` (`implemented`/`unknown`/`available`/`degraded`/`unavailable`), `effective_mode`, `effective_preset`, `reason`, `last_checked`, `elapsed_ms`, `trial_left`, `trial_limit`.

### `POST /v1/chat/completions`

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Alice-Session-ID: my-1' \
  -d '{"model":"alice-base","messages":[{"role":"user","content":"Привет"}],"stream":true}'
```

Поддерживается:
- `messages` с ролями `system`/`developer`/`user`/`assistant`/`tool`;
- `assistant.tool_calls` + `tool.tool_call_id` (полный continuation после результата инструмента);
- `stream: true` — `chat.completion.chunk` с дельтами, `finish_reason`, `[DONE]`;
- `tools` — определения функций (OpenAI формат), возвращаются как `tool_calls` с `id`, `type: "function"`, `function.name`, `function.arguments`;
- `tool_choice`: `auto` (по умолчанию), `none`, `required`, `{"type":"function","function":{"name":"..."}}`;
- `parallel_tool_calls`: `true`/`false` — при `false` и >1 вызовах возвращается 400 (non-stream) или SSE error (stream);
- `stream_options.include_usage: true` — финальный chunk с `usage: null`;
- `X-Alice-Session-ID` — изоляция истории.

**Сервер не исполняет инструменты**, переданные клиентом. Вызовы возвращаются клиенту (OpenCode, DeepSeek Harness, Cursor, Continue и т.д.), клиент исполняет и возвращает `tool` результат следующим сообщением.

**`tool_choice: "required"`** — один bounded retry (не более), если модель не вызвала инструмент. Если после retry вызова нет → `422 tool_choice_required_no_call`. Это не абсолютная гарантия модели, но контракт OpenAI-клиента при `required` соблюдается настолько, насколько позволяет протокол Алисы.

### `POST /v1/responses`

Responses API (новый формат OpenAI):
```bash
curl -X POST http://127.0.0.1:8000/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"alice-base","input":"Привет"}'
```

Поддерживается:
- `input` как `str` или `list` items (`message`, `function_call_output`);
- `instructions` — уходит как `[system]` в prompt;
- `tools` — Responses-формат (`{"type":"function","name","parameters"}`) и Chat-формат (`{"type":"function","function":{...}}`);
- `output` содержит `message` (с `output_text`) и `function_call` items (`call_id`, `name`, `arguments`);
- `stream: true` — события `response.created` → `output_item.added` → `output_text.delta` / `function_call_arguments.done` → `output_text.done` → `output_item.done` → `response.completed`;
- `previous_response_id` — реальная поддержка: response_id хранится в LRU+TTL (`ResponseStore`, TTL 1ч, 256 записей), продолжение идёт в тот же dialog_id; если ID не найден или истёк → `400 previous_response_not_found`;
- `store: false` — response_id не сохраняется (нельзя использовать в `previous_response_id`);
- `response_format`: `{"type":"text"}` no-op, `{"type":"json_object"}` → инструкция «valid JSON only» в prompt, `{"type":"json_schema","json_schema":{...}}` → schema + `strict` в prompt. Неизвестный тип → 400.

**Не поддерживается (возвращается ошибка или игнорируется):**
- `usage` — всегда `null` (у Алисы нет token counting). `null` лучше, чем фиктивное число.
- `response_format` с json_schema **не валидирует** ответ по схеме на сервере — только инструктирует модель. Клиент должен сам валидировать.
- `reasoning`, `truncation`, `background`, `prompt` — игнорируются.
- `previous_response_id` не хранит полную историю на сервере — только dialog_id; история диалога живёт в `AliceAgentManager`, не в gateway.

### Сессии

- Без `X-Alice-Session-ID` каждый запрос = новая сессия (uuid4) с ephemeral dialog_id. TTL 1ч, MAX 128, вытеснение LRU среди **неактивных**. Если все заняты — `500` с сообщением о capacity (backpressure, а не повреждение).
- С `X-Alice-Session-ID` — стабильная история в пределах сессии.
- Активные сессии (`_active > 0`) **не вытесняются** ни по TTL, ни по MAX.

### OpenAI Python SDK

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="any")

# Chat
r = client.chat.completions.create(
    model="alice-base",
    messages=[{"role": "user", "content": "Привет"}],
)

# Stream
for chunk in client.chat.completions.create(
    model="alice-base",
    messages=[{"role": "user", "content": "Привет"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")

# Tools
r = client.chat.completions.create(
    model="alice-base",
    messages=[{"role": "user", "content": "погода в Москве?"}],
    tools=[{"type": "function", "function": {
        "name": "get_weather",
        "parameters": {"type": "object",
                       "properties": {"city": {"type": "string"}}},
    }}],
)
if r.choices[0].finish_reason == "tool_calls":
    tc = r.choices[0].message.tool_calls[0]
    # клиент исполняет инструмент, затем:
    r2 = client.chat.completions.create(
        model="alice-base",
        messages=[
            {"role": "user", "content": "погода в Москве?"},
            {"role": "assistant", "content": None, "tool_calls": [tc]},
            {"role": "tool", "tool_call_id": tc.id, "content": '{"temp":5}'},
        ],
        tools=[...],
    )

# Responses
r = client.responses.create(model="alice-base", input="Привет")
print(r.output_text)
```

SDK-integration проверяется тестами `tests/test_openai_sdk_integration.py` (10 сценариев) против реального uvicorn-сервера.

## Архитектура

```
alice_auth.py        credentials, cookies/token parsing, atomic write 0600, healthcheck
alice_server.py      lifecycle, create_curl_http_app, main_async, main (230 строк)
alice_cli.py         интерактивный CLI
tool_bridge.py, expert_bridge.py, expert_browser.py, meeseeks.py, local_tools.py
agents/              встроенные агенты + registry (auto_discover по требованию)

tools/
  constants.py       протокол, uuid7
  parser.py          _parse_nsb_card, compute_delta, parse_rich_uicard
  effective.py       normalize_effective_mode, EffectiveMode
  transport.py       AliceRpcClient, AliceWsEngine (listener, supervisor, delta, upload)
  manager.py         GenericAppAgent, AliceAgentManager, ChatSession, SessionRegistry,
                     AliceMcpServer
  gateway.py         ModelCatalog, ResponseStore, /v1/*, _flatten_messages,
                     _extract_tool_calls, _inject_tool_instructions,
                     _validate_tool_request, _validate_response_format
```

`tools/*` **не импортирует** `alice_server` — циклов нет. `alice_server` реэкспортирует публичные классы для обратной совместимости.

**Протокол:**
- WebSocket: `SynchronizeState` → `ClientSubscriptionState` → `TextInput` → `VinsResponse`/`DeferredAliceResponse`. `DeferredAliceResponse` нормализуется через `compute_delta` (снимок/дельта/регресс/идентичный кадр).
- RPC: `/gproxy/get_alice_apps`, `/dialog/list`, `/dialog/read_dialog`, `/dialog/remove_dialog`.

`VinsResponse` нормализуется через `normalize_effective_mode`: top → nested → legacy → inline. `trial_state` обновляет `expert_trial_left/limit`. Поздние `VinsResponse` (после `finally` в `send_text_input`) игнорируются — не создают state.

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q tests/
```

Обычные тесты не требуют credentials и работают на фикстурах (`tests/fixtures/vins_*.json`, `deferred_*.json`) и fake-транспорте. SDK-integration тесты (`tests/test_openai_sdk_integration.py`) поднимают реальный uvicorn на свободном порту и проверяют через официальный `openai` SDK.

Live-тесты против настоящей Алисы не входят в обычный набор. Для них нужны рабочие credentials, отдельный профиль и разрешение.

## Известные ограничения

- **Pro → Base**: сервер Алисы может деградировать Pro в Base. `/v1/models/diagnostics` показывает `degraded`, `effective_mode: "Base"`. Не выдаём за подтверждённый Pro.
- **Expert trial ~5 запросов/24ч**. `/v1/models` не запускает healthcheck Expert чаще раза в сутки.
- **`usage` всегда `null`** — у Алисы нет token counting, фиктивное число было бы хуже.
- **`response_format: json_schema` не валидирует ответ** — только инструктирует модель. Клиент должен валидировать сам.
- **`tool_choice: "required"`** — bounded retry, но не абсолютная гарантия. Протокол Алисы не гарантирует вызов даже при явной инструкции.
- **`previous_response_id`** хранит только `dialog_id`, не полную историю. История живёт в `AliceAgentManager`.
- **Cookies нужны для safeclick и `get_source_file`** — операции «нажать кнопку в карточке» и «скачать файл» часто не работают только с OAuth.
- **`agents/lavka.py::_fetch_products`** — async через aiohttp, но реальный `productIds` от Лавки для проверки не предоставлялся.
- **Playwright** — опционален (`requirements-optional.txt`), без него fallback для скачивания файлов Expert недоступен.

## Лицензия

См. `LICENSE`. Проект не является официальным клиентом Яндекса. Пользователь самостоятельно проверяет применимые условия использования сервисов Яндекса.
