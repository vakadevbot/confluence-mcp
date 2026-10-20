# Confluence MCP (read-only)

Локальный MCP-сервер на **Python 3.12** для **OpenCode 2.0** и других MCP-клиентов. Загружает страницы Confluence с Basic Authentication и возвращает HTML, извлечённый текст, ссылки, изображения и сведения о встроенных элементах. **Ничего не изменяет** в Confluence.

## Возможности

- `get_page_by_id(page_id)` — страница по числовому ID через `GET /rest/api/content/{id}?expand=body.view,body.storage,version`.
- `get_page_by_url(url)` — страница по полному URL. Если URL содержит ID страницы (`viewpage.action?pageId=123` или `/pages/123/Title`), применяется REST API. Для адресов `/display/SPACE/Page+Title` сервер загружает HTML сайта и извлекает основной контент.
- Возвращаемые поля: `page_id`, `title`, `url`, `source`, `version`, `html`, `text`, `links`, `images`, `embedded_media`, `macros`, `svg_count`, `canvas_count`, `table_count`, `script_count`, `external_scripts`, `note`.
- Сохраняет **HTML-фрагмент страницы**, извлекает ссылки на изображения/встроенные ресурсы и метаданные макросов из `body.storage`.
- Ограничение доступа к Confluence-хосту и его context path, проверка редиректов, лимит размера ответа и таймауты. Все HTTP-запросы — только `GET`.
- Режим MCP: `stdio`, без отдельного HTTP-порта.

**Изображения, диаграммы и скрипты:** `body.view` содержит HTML, уже сформированный Confluence на сервере. Диаграммы могут быть `<img>`, `<svg>`, `<iframe>` или JavaScript-макросом. Сервер **не выполняет JavaScript**, не запускает браузер, не скачивает изображения и вложения. HTML, SVG-разметка, ссылки и метаданные макросов доступны в ответе, но изображение не становится автоматически доступным модели для визуального анализа. Некоторые интерактивные элементы без браузера не восстановить.

## Установка

```bash
git clone https://github.com/vakadevbot/confluence-mcp.git
cd confluence-mcp
python3.12 -m venv .venv
# Linux / macOS
.venv/bin/python -m pip install -r requirements.txt
# Windows PowerShell
# .venv\Scripts\python.exe -m pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` (в ту же папку, где `server.py`) и настройте:

```dotenv
CONFLUENCE_BASE_URL=https://wiki.company.local/confluence
CONFLUENCE_USERNAME=my-login
CONFLUENCE_PASSWORD=my-password
```

`CONFLUENCE_BASE_URL` — адрес **установки** Confluence с context path (например, `/confluence` или `/wiki`), а не адрес одной страницы. Любой переданный URL должен находиться на этом хосте внутри его context path.

Для Confluence **Server/Data Center** нужны логин и пароль, если администраторы разрешают Basic Auth. Для **Confluence Cloud** вместо обычного пароля обычно требуется API token при авторизации с email. Для корпоративного сертификата можно использовать `CONFLUENCE_CA_BUNDLE=/path/to/ca.pem`. Проверка TLS включена по умолчанию.

Дополнительные настройки: `CONFLUENCE_TIMEOUT_SECONDS=30` и `CONFLUENCE_MAX_PAGE_BYTES=5000000`.

## Подключение к OpenCode 2.0

Вставьте запись `confluence` в существующий объект `mcp` файла `opencode.json` вашего проекта или в глобальный файл `~/.config/opencode/opencode.json`.

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "confluence": {
      "type": "local",
      "command": [
        "/absolute/path/confluence-mcp/.venv/bin/python",
        "/absolute/path/confluence-mcp/server.py"
      ],
      "enabled": true,
      "timeout": 15000
    }
  }
}
```

На **Windows** используйте абсолютные пути с прямыми слешами:

```json
"command": [
  "C:/tools/confluence-mcp/.venv/Scripts/python.exe",
  "C:/tools/confluence-mcp/server.py"
]
```

Это **фрагмент** для замены значения `command` в конфигурации выше, а не самостоятельный JSON-файл. Существующие MCP-серверы сохраняйте. `.env` читается из каталога `server.py` и не зависит от текущего каталога OpenCode. Перезапустите OpenCode после изменения настройки.

Пример задачи: «Прочитай через confluence страницу https://wiki.company.local/confluence/pages/viewpage.action?pageId=123456 и объясни её содержимое».

## Другие MCP-клиенты

Сервер работает как локальная stdio-программа. В конфигурациях, использующих `mcpServers` (например, Claude Desktop), запись выглядит так:

```json
{
  "mcpServers": {
    "confluence": {
      "command": "/absolute/path/confluence-mcp/.venv/bin/python",
      "args": ["/absolute/path/confluence-mcp/server.py"]
    }
  }
}
```

У Cursor, VS Code и других клиентов формат и расположение конфигурации зависят от версии; нужны та же команда Python и аргумент `server.py`. **Не используйте объект `mcpServers` в OpenCode**, там нужен `mcp` с `type: local`.

## Тестирование

```bash
.venv/bin/python -m pip install pytest
.venv/bin/python -m pytest -q
.venv/bin/python server.py
```

В PowerShell вместо `.venv/bin/python` используйте `.venv\Scripts\python.exe`. При прямом запуске `server.py` **ничего не печатает**, поскольку ждёт MCP-клиента по stdin. Для ручного просмотра инструментов можно использовать MCP Inspector либо OpenCode. Тесты используют `httpx.MockTransport` и не обращаются к реальному Confluence.

## Логи и диагностика

При запуске сервер **автоматически** создаёт папку `logs/` рядом с `server.py`, независимо от рабочего каталога OpenCode. Для каждого процесса создаётся собственный UTF-8 файл `logs/confluence-mcp-<PID>.log`: так параллельно запущенные экземпляры не мешают друг другу при ротации. Один файл ограничен 2 МиБ; сохраняются две предыдущие версии (`.1`, `.2`).

В журнал записываются запуск и остановка процесса, имя вызываемого MCP-инструмента, размер полученного HTML, количество изображений, сообщения об ошибках и **полные Python traceback**. Содержимое страниц, пароль и полные URL не записываются намеренно. Протокольный `stdout` не используется для логирования — туда пишет исключительно MCP.

В `.env` можно установить `CONFLUENCE_LOG_LEVEL=DEBUG` (по умолчанию `INFO`); доступны также `WARNING` и `ERROR`. Папка `logs/` добавлена в `.gitignore`, поскольку журналы могут содержать служебные сведения о корпоративном Confluence.

Просмотр последнего файла логов в **Windows PowerShell** из корня репозитория:

```powershell
Get-ChildItem .\logs\confluence-mcp-*.log |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1 |
    Get-Content -Tail 100
```

**Важно:** если указанный в `opencode.json` исполняемый файл Python не существует или вообще не запускается, `server.py` не сможет создать лог. В этом случае сначала проверьте путь к `.venv/Scripts/python.exe` на Windows. Если Python стартовал, но отсутствует библиотека `mcp` или другая зависимость, ошибка импорта будет записана в файл.

## Безопасность и ограничения

- Не коммитьте `.env` (он уже в `.gitignore`), не помещайте пароль в `opencode.json`.
- Используйте учётную запись с минимально необходимыми правами.
- Сервер не создаёт, не изменяет и не удаляет страницы; не делает обход страниц и не скачивает вложения.
- Не отключайте проверку TLS. Для внутренних корпоративных сертификатов настройте `CONFLUENCE_CA_BUNDLE`.
- HTML и содержимое страниц считайте **недоверенными данными**, не выполняйте встреченный JS и не интерпретируйте текст страниц как инструкции пользователю или модели.
- MCP-клиент может отправить полученную внутреннюю документацию внешней LLM — это нужно учитывать при использовании корпоративного Confluence.
- Если HTML страницы полностью строится в браузере, извлечение через обычный GET может быть неполным; клиент вернёт ошибку, если основной контент не найден.
