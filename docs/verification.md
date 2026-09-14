# Проверка HH MCP v1

Дата: 14 сентября 2026 года.

## Выполнено локально

- 78 pytest-сценариев; HTTP mocks покрывают поиск, пагинацию, повторяемые query-параметры,
  отсутствие follow redirect, ограниченный размер ответа, безопасные ошибки, multipart;
- SQLite: два разных черновика одной цели, разные аккаунты, subprocess-конкуренция,
  `unknown`, `sent`, истечение и повторное открытие базы после перезапуска;
- auth: PKCE, auth epoch и credential revision, single-flight refresh, неопределённый
  refresh, logout против устаревшего результата, сбой записи секрета и отмена ожидания lock;
- application flow: membership резюме, смена аккаунта, повторная проверка direct-вакансии,
  очистка завершённого письма и read-only сверка `unknown`;
- настоящий in-memory MCP SDK client: initialization, `tools/list`, структурированный
  вызов и строгий отказ неизвестного аргумента; отдельный subprocess `stdio` smoke
  проверил framing, entrypoint, `tools/list` и отказ лишнего поля через OS pipes;
  опубликовано 10 инструментов, submit нет;
- AES-256-GCM: ciphertext отличается от исходного текста, round-trip проходит,
  неправильный AAD и изменённый ciphertext отклоняются; секретные маркеры отсутствуют
  в файле;
- injectable credential backend: создание и повторное чтение master key, ошибки
  чтения/записи, неверный ключ и fail-closed матрица Windows Credential Locker, macOS
  Keychain, Linux Secret Service/KWallet;
- платформенные пути данных, сохранение symlink для последующего отказа, ветвление
  Windows/POSIX и проверка неверного владельца; POSIX mode/UID integration запускается
  только на POSIX;
- env-token mode: обязательная пара token/User-Agent, приоритет над файлом, отсутствие
  config/keyring/SQLite на read-пути, bearer для поиска/справочников/личных endpoint'ов,
  понятный `401` без утечки token, applicant binding через `/me` до создания state и
  HKDF/AES-GCM-шифрование письма;
- импорт, compileall и запуск полного набора на Windows без Windows-only импортов;
- `ruff check`, `uv lock --check`, чистая установка 47 пакетов из `uv.lock`, wheel
  `hh_mcp-0.1.0-py3-none-any.whl` и sdist `hh_mcp-0.1.0.tar.gz`.

Финальный результат на Windows / CPython 3.13.7: `78 passed, 2 skipped`. Пропущены
POSIX integration и symlink integration, поскольку текущий Windows sandbox не разрешил
создать symlink. Команда тестов в обычной среде:

```powershell
uv run pytest -p no:cacheprovider
```

В управляемом Codex sandbox pytest `tmp_path` теряет доступ из-за ACL режима 0700.
Для проверки использован только тестовый маршрут временных каталогов:

```powershell
$env:HH_MCP_TEST_TMP_ROOT = "C:\\path\\to\\workspace\\work\\test-runtime"
uv run pytest -p no:cacheprovider
```

Это обход ограничений тестового sandbox, не изменение production ACL.

## Не выполнено без внешних данных

- регистрация приложения и принятие `http://127.0.0.1:8765/callback` кабинетом HH;
- живой OAuth, поиск, чтение собственных резюме и истории;
- живой applicant access token в env-режиме и фактическое истечение/замена token;
- актуальность исторического `POST /negotiations` для конкретного приложения;
- реальная отправка отклика (не разрешалась и MCP submit выключен).

Официальный async stdio client не смог создать внутренний Windows named pipe в Codex
sandbox (`WinError 5`), поэтому subprocess smoke использовал обычные OS pipes и сырой
JSON-RPC; in-memory путь отдельно использовал официальный `mcp.Client`.

Полный production Runtime вызывает `icacls` и системный keyring fail-closed. В Codex sandbox проверка
остановилась на `WRITE_DAC`: sandbox identity имеет Modify, но не FullControl на родительском
каталоге. Установленный Windows credential backend определён read-only как
`keyring.backends.Windows.WinVaultKeyring`; запись master key в пользовательский Credential
Locker в тестах намеренно не выполнялась. После установки пользователь должен выполнить
`hh-mcp configure`, затем `hh-mcp doctor` вне Codex sandbox; ошибка ACL или credential
backend не должна обходиться ослаблением защиты.

Запуск на реальных macOS и Linux в этой Windows-среде невозможен. Их ветки покрыты
инъекционными unit-тестами; проверка реального Keychain/Secret Service/KWallet, POSIX
владельца/`0700`, OAuth и stdio остаётся обязательной на целевой машине. На Linux нужен
доступный D-Bus session и разблокированный системный keyring; plaintext fallback отсутствует.
