#!/usr/bin/env python3
"""Persistent speaker driver for the Google Meet bot.

WHY THIS EXISTS
---------------
Driving a live call from an autonomous ``hermes chat -q`` agent fails two ways
(diagnosed 2026-06-22): (1) the agent hits ``agent.max_turns`` (~120 iterations)
in a few minutes of polling and the harness interrupts it, then the
``on_session_end`` hook kills the bot; (2) even before that, the autonomous agent
is unreliable — it wanders into meta-reasoning ("is audioBytesOut a bug?") instead
of speaking.

This driver inverts control: a deterministic Python loop OWNS the cadence and the
bot's lifetime; the LLM is called point-wise only to "produce the next reply" via
the warm in-process ``agent.auxiliary_client.call_llm`` (~3.6s/turn) — it can
neither run out of iterations nor wander. Heavy tasks run in a background thread
(also gpt-5.5) so the dialogue never blocks. Ends on the bot's graceful exit
(verbal_closure / alone), which triggers the usual auto-summary.

RUN: HERMES_HOME=<verter> python -m plugins.google_meet.meet_speaker_driver <meet-url>
Model via env DRIVER_MODEL / DRIVER_PROVIDER (default gpt-5.5 / openai-codex).
"""
from __future__ import annotations

import os
import sys
import time
import threading

_REPO = "/home/vitaly/.hermes/hermes-agent"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

os.environ.setdefault("HERMES_HOME", "/home/vitaly/.hermes/profiles/verter")

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402
load_hermes_dotenv()  # bring HERMES_MEET_* (profile .env) into env for the bot

from plugins.google_meet import tools, process_manager as pm  # noqa: E402
from agent.auxiliary_client import call_llm  # noqa: E402
from agent.plugin_llm import _extract_text  # noqa: E402

MODEL = os.environ.get("DRIVER_MODEL", "gpt-5.5")
PROVIDER = os.environ.get("DRIVER_PROVIDER", "openai-codex")
GUEST = os.environ.get("HERMES_MEET_GUEST_NAME", "Verter Multitender")
POLL_SEC = float(os.environ.get("DRIVER_POLL_SEC", "2.5"))
MAX_CTX_LINES = 24            # rolling dialogue context fed to the LLM
HEAVY_TIMEOUT = float(os.environ.get("DRIVER_HEAVY_TIMEOUT", "600"))

_SYS = (
    "Ты — Verter, голосовой ассистент на Google Meet созвоне. Отвечай по-русски, "
    "живо и разговорно, 1-3 коротких предложения (это произносится вслух — без "
    "разметки, списков, ссылок). Тебе дают последние реплики участников.\n"
    "Реши по последней реплике человека:\n"
    "- Если участники говорят между собой и тебе вмешиваться НЕ нужно → ответь "
    "РОВНО: SKIP\n"
    "- Если это тяжёлая задача (поиск, расчёт, анализ, написать код/документ, "
    "долгое действие) → ответь РОВНО одной строкой: [DELEGATE] <короткое "
    "описание задачи своими словами>\n"
    "- Иначе → дай короткий живой устный ответ.\n"
    "Никогда не описывай свои действия, просто говори как человек."
)


def _log(out_dir, msg: str) -> None:
    try:
        with (out_dir / "driver.log").open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _say(text: str) -> None:
    text = (text or "").strip()
    if text:
        tools.handle_meet_say({"text": text})


def _llm(messages, model=MODEL, timeout=60) -> str:
    try:
        r = call_llm(provider=PROVIDER, model=model, messages=messages, timeout=timeout)
        return _extract_text(r).strip()
    except Exception as e:  # never crash the loop on a model hiccup
        return f"__ERR__:{type(e).__name__}:{e}"


def _is_self(speaker: str) -> bool:
    s = (speaker or "").strip().lower()
    return (not s) or s == "you" or s == GUEST.strip().lower()


def _greeting(status) -> str:
    names = [n for n in (status.get("participantNames") or []) if not _is_self(n)]
    count = status.get("participantCount")
    msgs = [
        {"role": "system", "content": _SYS},
        {"role": "user", "content": (
            f"Ты только что подключился к созвону. Участников примерно: {count}. "
            f"Имена говоривших: {', '.join(names) if names else 'неизвестны'}. "
            "Поздоровайся как живой человек одной короткой фразой "
            "(одного — по имени если есть; нескольких — поприветствуй команду). "
            "Только сама фраза приветствия.")},
    ]
    g = _llm(msgs, timeout=40)
    if g.startswith("__ERR__") or not g:
        g = "Всем привет! Я Verter, на связи — чем могу помочь?"
    return g


def _run_heavy(task: str, context: str, out_dir) -> None:
    _log(out_dir, f"HEAVY start: {task[:80]}")
    msgs = [
        {"role": "system", "content": (
            "Ты — Verter, выполняешь задачу с созвона в фоне. Верни КРАТКИЙ "
            "результат (2-5 предложений) для зачитывания вслух, по-русски, без "
            "разметки.")},
        {"role": "user", "content": f"Задача: {task}\n\nКонтекст созвона:\n{context}"},
    ]
    out = _llm(msgs, timeout=HEAVY_TIMEOUT)
    if out.startswith("__ERR__") or not out:
        _say(f"Не получилось доделать задачу: {task[:60]}.")
        _log(out_dir, f"HEAVY fail: {out[:120]}")
        return
    _say("По задаче готово. " + out)
    _log(out_dir, f"HEAVY done: {out[:120]}")


def main(argv) -> int:
    url = argv[1] if len(argv) > 1 else os.environ.get("HERMES_MEET_URL", "")
    if not url:
        print("usage: meet_speaker_driver <meet-url>", file=sys.stderr)
        return 2

    res = tools.handle_meet_join({"url": url, "mode": "realtime"})
    import json as _j
    out_dir = None
    try:
        out_dir = __import__("pathlib").Path(_j.loads(res).get("out_dir"))
    except Exception:
        pass
    if out_dir is None:
        print(f"join failed: {res}", file=sys.stderr)
        return 1
    _log(out_dir, f"driver up: model={MODEL} url={url}")

    # 1) Wait for realtime readiness (max ~90s).
    deadline = time.time() + 90
    while time.time() < deadline:
        st = pm.status()
        if st.get("realtimeReady") and st.get("inCall"):
            break
        if st.get("exited"):
            _log(out_dir, "bot exited before ready")
            return 1
        time.sleep(2)

    # 2) Greet once.
    st = pm.status()
    _say(_greeting(st))
    _log(out_dir, "greeted")

    # 3) Conversation loop — deterministic cadence, point-wise LLM calls.
    cursor = -1
    convo: list = []          # rolling "Speaker: text" of the whole dialogue
    pending_human = False     # new human content awaiting a reply
    while True:
        st = pm.status()
        if st.get("exited") or st.get("leaveReason"):
            _log(out_dir, f"end: leaveReason={st.get('leaveReason')} exited={st.get('exited')}")
            break

        tr = pm.transcript(since_id=cursor if cursor >= 0 else None)
        new = tr.get("cleanLines") or []
        ids = tr.get("cleanLineIds") or []
        if isinstance(tr.get("maxCleanId"), int) and tr["maxCleanId"] >= 0:
            cursor = tr["maxCleanId"]
        for line in new:
            speaker = line.split(":", 1)[0].strip() if ":" in line else ""
            if _is_self(speaker):
                continue          # ignore our own TTS echo
            convo.append(line)
            pending_human = True
        convo = convo[-MAX_CTX_LINES:]

        if pending_human:
            pending_human = False
            msgs = [
                {"role": "system", "content": _SYS},
                {"role": "user", "content": (
                    "Диалог на созвоне (последние реплики):\n"
                    + "\n".join(convo) + "\n\nТвой ход:")},
            ]
            reply = _llm(msgs, timeout=60)
            if reply.startswith("__ERR__"):
                _log(out_dir, f"llm err: {reply[:120]}")
            elif reply.strip().upper() == "SKIP":
                _log(out_dir, "skip (humans talking)")
            elif reply.strip().startswith("[DELEGATE]"):
                task = reply.split("]", 1)[1].strip() if "]" in reply else reply
                _say(f"Принял — делаю: {task}. Вернусь с результатом.")
                threading.Thread(target=_run_heavy,
                                 args=(task, "\n".join(convo), out_dir),
                                 daemon=True).start()
                _log(out_dir, f"delegated: {task[:80]}")
            else:
                _say(reply)
                _log(out_dir, f"said: {reply[:80]}")

        time.sleep(POLL_SEC)

    # 4) Graceful stop (bot may already be gone; this is idempotent → summary).
    try:
        if pm.status().get("alive"):
            pm.stop(reason="driver end")
    except Exception:
        pass
    _log(out_dir, "driver done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
