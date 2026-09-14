# Матрица контрактов HH MCP v1

Проверено 14 сентября 2026 года. Текущий OpenAPI: официальный
[`https://api.hh.ru/openapi/specification/public`](https://api.hh.ru/openapi/specification/public),
SHA-256 `AD3B333DB566D474F6610753393F14B40F569DD9E505C4028791DC7068C88D59`.

| Возможность | HH endpoint | Основание | Статус |
|---|---|---|---|
| Пользователь/роль | `GET /me` | текущий OpenAPI | подтверждён |
| Токен/refresh/app token | `POST /token` | текущий OpenAPI | подтверждён |
| Поиск | `GET /vacancies` | текущий OpenAPI | подтверждён |
| Вакансия | `GET /vacancies/{vacancy_id}` | текущий OpenAPI | подтверждён |
| Работодатель | `GET /employers/{employer_id}` | текущий OpenAPI | подтверждён |
| Справочники | `GET /areas`, `/professional_roles`, `/metro`, `/industries`, `/dictionaries` | текущий OpenAPI | подтверждён |
| Мои резюме | `GET /resumes/mine` | текущая ссылка `resumes_url` из `/me`; [официальная историческая схема](https://github.com/hhru/api/blob/2e65e6ef8d39b1904ce57d5ff50d9ab8c2ca8faa/docs/resumes.md) | маршрут подтверждён, текущая схема ответа не полностью проверена |
| Моё резюме | `GET /resumes/{resume_id}` + membership через `/mine` | текущий OpenAPI для карточки; `/mine` как выше | проверяется владельцем локально |
| История | `GET /negotiations` | текущий OpenAPI | подтверждён для чтения; живой applicant-ответ не проверен |
| Деталь истории | `GET /negotiations/{id}` | текущий OpenAPI | подтверждён для чтения; живой applicant-ответ не проверен |
| Создание отклика | `POST /negotiations` multipart | [официальный исторический документ HH](https://github.com/hhru/api/blob/fdb7b1803ddf0491bdce0b5d4387ebd01a0f78ec/docs/negotiations.md), commit до миграции OpenAPI, 2023-12-06 | текущий write-контракт не подтверждён; MCP-инструмент выключен |

Write-путь понимает исторические `201 Location` и `303` без следования redirect, не
повторяет POST и переводит неопределённый результат в устойчивый `unknown`. Это не
обещание идемпотентности HH и не доказательство доставки точного письма по истории.

Loopback callback `http://127.0.0.1:8765/callback` соответствует реализованной модели,
но его принятие конкретным приложением подтверждается только при регистрации/живом
OAuth. MCP v2 предоставляет elicitation/Resolve, однако конкретная внешняя политика
клиента, связывающая решение человека с неизменяемым содержимым отправки, здесь не
доказана; поэтому `hh_submit_application` не регистрируется.

Платформенный слой не меняет HTTP-контракты HH: тот же loopback IPv4 listener и stdio
используются на Windows, macOS и Linux. Локальные секреты защищены AES-256-GCM, а master
key хранится в системном credential service; неподдерживаемый или небезопасный backend
останавливает Runtime до чтения личных методов.

Альтернативный env-token режим использует один переданный пользователем applicant OAuth
access token для всех read-endpoint'ов. Перед созданием черновика `GET /me` подтверждает
роль `applicant` и `account_id`; это не добавляет новый HH-контракт. Env-режим не делает
refresh и интерпретирует `401` как необходимость заменить `HH_MCP_ACCESS_TOKEN`.
