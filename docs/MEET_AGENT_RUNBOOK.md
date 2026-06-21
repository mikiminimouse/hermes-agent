# Google Meet bot — agent runbook (как Гермесу самостоятельно вести встречу)

Этот документ — единственный источник правды для агента, который управляет
Google Meet ботом через штатные инструменты `meet_join / meet_status /
meet_transcript / meet_say / meet_leave`. Прочитай его целиком ПЕРЕД запуском —
он закрывает все грабли, на которые натыкались раньше.

Профиль прода — `verter`. Код живёт на ветке `main` форка
`mikiminimouse/hermes-agent` (durable: переживает `hermes update`, см.
`docs/MEET_REFERENCE_TODO.md`). Все meet-настройки приходят из systemd drop-in
`~/.config/systemd/user/hermes-gateway-verter.service.d/meet.conf`.

---

## 1. Что бот умеет и как устроен

Поток: `meet_join` → бот стартует как **detached subprocess** (`meet_bot.py`),
открывает **настоящий Chrome** с залогиненным профилем (`meet-google`) через
DE-egress-прокси → жмёт «Присоединиться/Join» → **ждёт, пока хост впустит** →
включает русские субтитры → пишет `transcript.txt` → при graceful-конце пишет
`report.md`.

Бот общается с агентом ТОЛЬКО через файлы в каталоге встречи (`out_dir`):
`status.json`, `transcript.txt`, `bot.log`, `admission_debug.json`,
`summary_request.json`, `summary.log`, `report.md`.

Ключевой код: `plugins/google_meet/meet_bot.py`
- `run_bot()` — навигация + join (`_click_join` ~872), установка caption-observer
- admission-цикл ~983: `_detect_admission` (~1306), `_detect_denied`, lobby-timeout
- `_dump_admission_snapshot` (~1266) — пишет `admission_debug.json`
- авто-конец `_detect_alone` (~1360), summary-сигнал ~1195

---

## 2. КРИТИЧНО: admission требует человека-хоста

Бот **не может сам себя впустить**. После клика «Присоединиться» Google держит
его в лобби, пока **хост вручную не нажмёт «Впустить/Admit»**. Авто-впуск
бывает только если аккаунт бота (`meet-google` профиль) приглашён в встречу
того же Google Workspace.

Поэтому корректный тест/встреча ВСЕГДА требует, чтобы:
1. встреча была **реально запущена** (кто-то в ней есть как хост), и
2. хост **впустил бота** из лобби в течение окна ожидания.

Если этого не сделать — `inCall` НИКОГДА не станет `true`, и это **не баг бота**.
Не трать на это admission-таймаут: сначала убедись, что хост готов впускать.

---

## 3. Машина состояний — поля `status.json` (читай через `meet_status`)

| Поле | Значение |
|------|----------|
| `alive` | процесс бота жив |
| `joinAttemptedAt` | бот нажал кнопку входа (≠ вошёл) |
| `lobbyWaiting` | бот в лобби, ждёт впуска |
| `inCall` | **true = бот реально в звонке** (цель) |
| `joinedAt` | момент впуска (null = ещё не впущен) |
| `captioning` | субтитры включены и читаются |
| `transcriptLines` | сколько реплик уже захвачено |
| `error` | текст ошибки или null |
| `leaveReason` | причина выхода: `alone` / `duration_expired` / `meet_leave` / `page_closed` / `denied` / `lobby_timeout` / null |
| `exited` | бот завершился |

Цель успешного входа: **`inCall=true` + `joinedAt` ≠ null**. Только после этого
ждать `transcriptLines > 0` (когда люди говорят).

---

## 4. Диагностика сбоя входа — ЧИТАЙ `admission_debug.json`

Бот **всегда** (без всяких флагов) пишет в `out_dir/admission_debug.json`
снапшот того, что Meet показывает на экране, с полем `note`:
- `note="waiting for admission"` — бот в лобби, ждёт впуска. Смотри `text`:
  если там «Попросить присоединиться/Ask to join», «подождите, пока вас
  впустят» — всё ок, **нужно чтобы хост впустил**.
- `note="join button not clicked"` — кнопка входа не нажалась. Смотри `buttons`:
  если «Ask to join» с `disabled:true` — обычно **не заполнено имя**; если
  «You can't join this video call… admitted by host» — **встреча закрытая**.
- `note="host denied admission"` — хост **отклонил** бота (`leaveReason=denied`).
- `note="lobby timeout"` — хост не впустил за окно (`leaveReason=lobby_timeout`).

Дополнительно, для подробного потока в `bot.log`, можно (необязательно) выставить
`HERMES_MEET_DEBUG_MODE=1` — но `admission_debug.json` есть всегда, начни с него.

Важно: **`captioning=true` при `inCall=false`** — это не «почти вошёл». Это
промежуточное состояние; верь `inCall`/`joinedAt`, а причину смотри в
`admission_debug.json`.

---

## 5. Авто-отчёт (summary) — когда и как

Срабатывает ТОЛЬКО при graceful-конце (`leaveReason` ∈
`{alone, duration_expired, meet_leave, page_closed, null}`) И если бот реально
был в звонке (`joinedAt` ≠ null) И есть субтитры (`transcriptLines > 0`).
`denied`/`lobby_timeout` отчёт НЕ порождают (нечего суммировать).

Цепочка: бот пишет `summary_request.json` (status=`requested`) → detached
запускает `HERMES_MEET_SUMMARY_CMD` (=`run_summary.sh`) с `out_dir` → тот
схлопывает растущие субтитры и зовёт `codex` (без API-ключа, через nvm) →
пишет `report.md` → переводит маркёр в `done`/`failed`. Идёт ~1-3 минуты.

Проверка после конца встречи:
1. `summary_request.json` → `status` стал `done` (или `failed`)
2. `summary.log` → лог работы (при `failed` — причина здесь)
3. `report.md` → итоговый отчёт (прочитать и показать, дать абсолютный путь)

Авто-конец пустой встречи включён по умолчанию: когда после того как кто-то
был в звонке, бот остаётся один на `HERMES_MEET_ALONE_TIMEOUT` (90с) → выходит
с `leaveReason=alone` → запускает summary сам.

---

## 6. Правильная процедура E2E-теста (делай ИМЕННО так)

0. Убедись, что нет висящей встречи: `meet_status` → если active, `meet_leave`.
1. Договорись, что **человек будет хостом и впустит бота**. Без этого тест
   бессмысленен. Используй СВЕЖУЮ ссылку реально запущенной встречи (НЕ старый
   дефолт `jvg-jqig-rbf`).
2. `meet_join {url, mode:"transcribe"}`. Запиши `out_dir`.
3. Polling `meet_status` каждые ~10с. **Окно admission — минимум 3-4 минуты**,
   а не 2: человеку нужно успеть нажать «Впустить». Параллельно читай
   `admission_debug.json` — там видно, ждёт ли бот впуска.
4. Как только `inCall=true` и `joinedAt`≠null — попроси хоста проговорить
   2-4 фразы по-русски. Читай `meet_transcript` каждые ~15с до `transcriptLines>0`.
5. Заверши: `meet_leave` (graceful, `leaveReason=null`).
6. Дождись `report.md` в `out_dir` (~1-3 мин), проверяя `summary_request.json`
   (status→done) и `summary.log`. Прочитай `report.md`, покажи путь.
7. Если `report.md` нет за 4 мин или status=failed — покажи `summary.log` и
   `transcript.txt` целиком.

Не запускай подагентов и внешние API — только штатные meet-инструменты и чтение
файлов в `out_dir`.

---

## 7. Частые ошибки (НЕ повторяй)

- **Слишком короткое окно admission.** 2 минуты мало — хост не успевает впустить.
  Жди 3-4 минуты и СНАЧАЛА убедись, что хост готов.
- **Старый/мёртвый URL.** `jvg-jqig-rbf` — это захардкоженный дефолт из тестового
  стенда, не живая встреча. Всегда бери свежую ссылку.
- **Считать `captioning=true` входом.** Вход — это `inCall=true`/`joinedAt`.
- **Гадать о причине вместо чтения `admission_debug.json`.** Снапшот есть всегда.
- **Ждать отчёт после denied/lobby_timeout.** Его не будет — это не graceful-конец.
- **Бросать висящий процесс.** После теста всегда `meet_leave`; проверь, что
  `exited=true` и нет осиротевшего `meet_bot`/Chrome.
