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
import re
import sys
import time
import difflib
import threading
import subprocess

_REPO = "/home/vitaly/.hermes/hermes-agent"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

os.environ.setdefault("HERMES_HOME", "/home/vitaly/.hermes/profiles/verter")

from hermes_cli.env_loader import load_hermes_dotenv  # noqa: E402
load_hermes_dotenv()  # bring HERMES_MEET_* (profile .env) into env for the bot

from plugins.google_meet import tools, process_manager as pm  # noqa: E402
from plugins.google_meet.meet_bot import _is_farewell_candidate  # noqa: E402
from agent.auxiliary_client import call_llm  # noqa: E402
from agent.plugin_llm import _extract_text  # noqa: E402

# ---------------------------------------------------------------------------
# Tunable thresholds (THRESHOLDS). Every magic number lives here with its
# rationale + an env override, so the driver can be tuned on a live call without
# a code edit. Values are validated on import (fail fast on a typo'd env), so an
# out-of-range override raises instead of silently degrading. See
# docs/MEET_AGENT_RUNBOOK.md §6g.
# ---------------------------------------------------------------------------
def _envf(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = float(default)
    if not (lo <= v <= hi):
        raise ValueError(f"{name}={v} out of range [{lo}, {hi}]")
    return v


def _envi(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        v = int(default)
    if not (lo <= v <= hi):
        raise ValueError(f"{name}={v} out of range [{lo}, {hi}]")
    return v


MODEL = os.environ.get("DRIVER_MODEL", "gpt-5.5")
PROVIDER = os.environ.get("DRIVER_PROVIDER", "openai-codex")
GUEST = os.environ.get("HERMES_MEET_GUEST_NAME", "Verter Multitender")

# Loop cadence: how often we poll status()/transcript(). 2.5s balances
# responsiveness vs. status() churn; below ~1s adds load with no perceptible gain.
POLL_SEC = _envf("DRIVER_POLL_SEC", 2.5, 0.5, 30)
MAX_CTX_LINES = _envi("DRIVER_MAX_CTX_LINES", 24, 4, 200)  # dialogue lines → LLM

# Address fuzzy-match ratio: a word must START like the name (вер/вэр/вёр) AND
# score >= this vs. "вертер". 0.8 catches ASR manglings (вертел/вертера) while
# weather words (ветер/ветра, prefix вет-) are excluded by the prefix guard. The
# strict LLM prompt is the SECOND filter. NB: this is NOT REPLY_DEDUP_RATIO —
# older comments drifted and conflated the two (0.8 here vs 0.72 there).
ADDRESS_FUZZY_RATIO = _envf("DRIVER_ADDRESS_FUZZY_RATIO", 0.8, 0.5, 1.0)

# End-of-meeting from the DIALOGUE (not the fragile DOM participantCount, which
# returned None in the field): after a farewell, leave once humans go quiet for
# END_GRACE; backstop — leave after MAX_IDLE of NO human speech at all (0
# disables). Independent of _detect_alone/participantCount.
END_GRACE = _envf("DRIVER_END_GRACE", 25, 0, 600)
MAX_IDLE = _envf("DRIVER_MAX_IDLE", 300, 0, 3600)

# Reply-dedup: don't re-voice a reply too similar to a recent one — stops
# "да, я на связи" cycling when the user re-asks. RATIO = similarity threshold;
# WINDOW = lookback seconds (B3 narrows the policy to the last few replies).
REPLY_DEDUP_RATIO = _envf("DRIVER_REPLY_DEDUP_RATIO", 0.72, 0.5, 1.0)
REPLY_DEDUP_WINDOW = _envf("DRIVER_REPLY_DEDUP_WINDOW", 90, 0, 3600)

# Heavy background task: timeout for the tool-capable agent + max concurrent.
HEAVY_TIMEOUT = _envf("DRIVER_HEAVY_TIMEOUT", 900, 30, 3600)
MAX_HEAVY = _envi("DRIVER_MAX_HEAVY", 1, 1, 8)
# PATH for the background tool-capable agent (hermes CLI + nvm node for codex).
_HERMES_BIN = os.environ.get(
    "DRIVER_HERMES_BIN",
    "/home/vitaly/.local/bin:/home/vitaly/.nvm/versions/node/v24.13.1/bin")


# Address detection: the bot responds only when called by name. Russian ASR
# routinely mangles "Вертер" (heard "ветра", "ветер", "вертел"…), so we fuzzy-
# match each word against "вертер". The strict LLM prompt is the second filter —
# if a near-miss like "ветер" (wind) wasn't really an address, the model returns
# SKIP. Greeting on join and goodbye on close are the only unprompted lines.
def _addressed(text: str) -> bool:
    t = (text or "").lower()
    if "verter" in t or "вертер" in t or "вэртер" in t:
        return True
    for w in re.findall(r"[а-яёa-z]{5,9}", t):
        # Must START like the name (вер/вэр/вёр). Weather words "ветер"/"ветра"
        # begin with "вет-" and would otherwise fuzzy-match high. Clean
        # "вертер"/"verter" already caught above.
        if w[:3] in ("вер", "вэр", "вёр") and \
                difflib.SequenceMatcher(None, w, "вертер").ratio() >= ADDRESS_FUZZY_RATIO:
            return True
    return False

# Background-task throttle: ONE tool-capable agent at a time + dedup of
# near-identical tasks, so a topic discussed repeatedly (with the bot named) does
# NOT spawn a swarm of hermes -z agents (live test spawned 13 in 2 min).
_heavy_lock = threading.Lock()
_heavy_active = 0
_recent_tasks: list = []


def _norm_task(t: str) -> str:
    return re.sub(r"[^\w\s]", " ", (t or "").lower())


def _try_delegate(task: str, context: str, out_dir) -> str:
    """Spawn a background tool-capable agent for *task* unless one is already
    running or the task duplicates a recent one. Returns the line to speak."""
    global _heavy_active
    nt = _norm_task(task)
    with _heavy_lock:
        for prev in _recent_tasks[-6:]:
            if difflib.SequenceMatcher(None, nt, prev).ratio() >= 0.6:
                return "Эту задачу я уже взял в работу — скоро вернусь с результатом."
        if _heavy_active >= MAX_HEAVY:
            return "Я ещё занят предыдущей задачей — доделаю её и сразу возьму эту."
        _heavy_active += 1
        _recent_tasks.append(nt)

    def _worker():
        global _heavy_active
        try:
            _run_heavy(task, context, out_dir)
        finally:
            with _heavy_lock:
                _heavy_active -= 1

    threading.Thread(target=_worker, daemon=True).start()
    return f"Принял — делаю: {task}. Вернусь с результатом."

_SYS = (
    "Ты — Verter, голосовой ассистент на Google Meet созвоне. Отвечай по-русски, "
    "живо и разговорно, 1-3 коротких предложения (это произносится вслух — без "
    "разметки, списков, ссылок).\n"
    "ГЛАВНОЕ ПРАВИЛО: ты реагируешь ТОЛЬКО когда обращаются ЛИЧНО к тебе — по "
    "имени «Вертер»/«Verter» или явной просьбой к тебе («Вертер, сделай…», "
    "«спроси у Вертера…»). Если люди просто разговаривают между собой, обсуждают "
    "что-то НЕ обращаясь к тебе — ты МОЛЧИШЬ.\n"
    "Реши по последней реплике:\n"
    "- Обращения к тебе нет / это разговор людей между собой → ответь РОВНО: SKIP\n"
    "- Тебя ЯВНО попросили ВЫПОЛНИТЬ конкретное тяжёлое действие (найди, проверь, "
    "посчитай, настрой, напиши код/документ) → ответь РОВНО одной строкой: "
    "[DELEGATE] <короткое описание задачи своими словами>. НЕ делегируй, если "
    "тему просто обсуждают или уже просили это же — тогда обычный ответ или SKIP.\n"
    "- К тебе обратились с обычным вопросом/репликой → дай короткий живой устный "
    "ответ.\n"
    "Никогда не описывай свои действия и НЕ оправдывайся (не говори «я молчал, "
    "потому что…», «не поздоровался, потому что…») — просто отвечай по сути, "
    "по-разному, не повторяй одну и ту же фразу."
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
    """Run a heavy task in the background with a TOOL-CAPABLE agent (hermes -z:
    web search, files, shell, skills) so it actually DOES the work, not just
    reasons. Slow (full agent), but it's off the dialogue path; the result is
    spoken when ready. Falls back to a plain LLM call if the CLI is unavailable."""
    _log(out_dir, f"HEAVY start (agent): {task[:80]}")
    prompt = (
        "Ты — Verter, выполняешь задачу, поставленную на голосовом созвоне. "
        "У тебя ЕСТЬ инструменты (поиск в сети, чтение файлов, выполнение команд, "
        "навыки) — ИСПОЛЬЗУЙ их, чтобы реально выполнить задачу, а не просто "
        "рассуждать о ней.\n\n"
        f"Задача: {task}\n\nКонтекст созвона (последние реплики):\n{context}\n\n"
        "В КОНЦЕ верни краткий результат (2-5 предложений) для зачитывания вслух "
        "по-русски, без разметки и ссылок."
    )
    out = ""
    try:
        env = dict(os.environ)
        env["PATH"] = _HERMES_BIN + ":" + env.get("PATH", "")
        r = subprocess.run(
            ["hermes", "-z", prompt, "-m", MODEL, "--profile", "verter", "--yolo"],
            capture_output=True, text=True, timeout=HEAVY_TIMEOUT, env=env)
        out = (r.stdout or "").strip()
        if not out:
            _log(out_dir, f"HEAVY agent empty; stderr={r.stderr[-200:] if r.stderr else ''}")
    except subprocess.TimeoutExpired:
        _log(out_dir, "HEAVY agent timeout")
    except FileNotFoundError:
        # No hermes CLI on PATH — degrade to an LLM-only answer.
        out = _llm([
            {"role": "system", "content": "Ты Verter. Кратко, по-русски, для речи."},
            {"role": "user", "content": f"Задача: {task}\nКонтекст:\n{context}"},
        ], timeout=HEAVY_TIMEOUT)
        if out.startswith("__ERR__"):
            out = ""
    if not out:
        _say(f"Не получилось доделать задачу: {task[:60]}.")
        _log(out_dir, "HEAVY fail")
        return
    _say("По задаче готово. " + out[:700])
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

    # 1) Wait until ADMITTED (realtimeReady AND inCall). Do NOT greet into the
    # lobby: if the host never admits, the bot lobby-times-out and we just exit
    # cleanly — greeting an un-admitted bot only voiced into the void.
    admit_wait = float(os.environ.get("DRIVER_ADMIT_WAIT", "330"))
    admit_deadline = time.time() + admit_wait
    admitted = False
    while time.time() < admit_deadline:
        st = pm.status()
        if st.get("realtimeReady") and st.get("inCall"):
            admitted = True
            break
        if st.get("exited") or st.get("leaveReason"):
            _log(out_dir, f"bot ended before admission: {st.get('leaveReason')}")
            return 1
        time.sleep(2)
    if not admitted:
        _log(out_dir, "not admitted within wait window — leaving without greeting")
        try:
            if pm.status().get("alive"):
                pm.stop(reason="not admitted")
        except Exception:
            pass
        return 1

    # 2) Greet — and VERIFY it was actually voiced. Even with the Silero warm-up
    # in connect(), the audio pipeline (pump/sink) can need a moment, so retry up
    # to 3x, each time confirming audioBytesOut actually grew before moving on.
    st = pm.status()
    greeting = _greeting(st)
    for attempt in range(3):
        before = pm.status().get("audioBytesOut") or 0
        _say(greeting)
        _log(out_dir, f"greet attempt {attempt + 1}")
        voiced = False
        for _ in range(8):                     # wait up to ~8s for audio to flow
            time.sleep(1)
            if (pm.status().get("audioBytesOut") or 0) > before:
                voiced = True
                break
        if voiced:
            _log(out_dir, "greeting voiced ✓")
            break
        _log(out_dir, "greeting produced no audio — retrying")

    # 3) Conversation loop — deterministic cadence, point-wise LLM calls.
    cursor = -1
    convo: list = []          # rolling "Speaker: text" of the whole dialogue
    addressed = False         # bot was explicitly addressed in the new lines
    farewelled = False        # we've already said our goodbye
    recent_replies: list = []   # [(norm, at)] for reply-dedup across last few
    last_human_at = time.time()   # presence signal: when a human last spoke
    had_human = False
    while True:
        st = pm.status()
        if st.get("exited") or st.get("leaveReason"):
            _log(out_dir, f"end: leaveReason={st.get('leaveReason')} exited={st.get('exited')}")
            break

        tr = pm.transcript(since_id=cursor if cursor >= 0 else None)
        new = tr.get("cleanLines") or []
        if isinstance(tr.get("maxCleanId"), int) and tr["maxCleanId"] >= 0:
            cursor = tr["maxCleanId"]
        closing = False
        addressed = False   # per-batch: reset every tick so it never lingers
        for line in new:
            speaker = line.split(":", 1)[0].strip() if ":" in line else ""
            text = line.split(":", 1)[1].strip() if ":" in line else line
            if _is_self(speaker):
                continue          # ignore our own TTS echo
            convo.append(line)
            last_human_at = time.time()   # a human just spoke → presence
            had_human = True
            if _addressed(text):          # bot called by name (ASR-fuzzy)
                addressed = True
            if _is_farewell_candidate(text):
                closing = True
        convo = convo[-MAX_CTX_LINES:]

        # Farewell is an EXCEPTION to address-gating: finish our speech before
        # the meeting closes (greeting + goodbye are the only unprompted lines).
        if closing and not farewelled:
            farewelled = True
            bye = _llm([
                {"role": "system", "content": _SYS},
                {"role": "user", "content": (
                    "Встреча завершается, участники прощаются. Скажи короткое "
                    "тёплое прощание одной фразой. Только фраза.")},
            ], timeout=40)
            if bye.startswith("__ERR__") or not bye:
                bye = "Спасибо всем, до связи!"
            _say(bye)
            _log(out_dir, f"farewell: {bye[:60]}")

        # Normal turns: respond ONLY when explicitly addressed (by name / wake).
        elif addressed:
            addressed = False
            reply = _llm([
                {"role": "system", "content": _SYS},
                {"role": "user", "content": (
                    "Диалог на созвоне (последние реплики):\n"
                    + "\n".join(convo) + "\n\nК тебе обратились. Твой ход:")},
            ], timeout=60)
            if reply.startswith("__ERR__"):
                _log(out_dir, f"llm err: {reply[:120]}")
            elif reply.strip().upper() == "SKIP":
                _log(out_dir, "skip (not really for me)")
            elif reply.strip().startswith("[DELEGATE]"):
                task = reply.split("]", 1)[1].strip() if "]" in reply else reply
                msg = _try_delegate(task, "\n".join(convo), out_dir)
                _say(msg)
                _log(out_dir, f"delegate→ {msg[:32]} | {task[:60]}")
            else:
                # Reply-dedup across the last few replies (not just the last) so
                # an A/B/A/B cycle of near-identical answers can't slip through.
                now2 = time.time()
                rnorm = _norm_task(reply)
                recent_replies = [(t, a) for (t, a) in recent_replies
                                  if now2 - a < REPLY_DEDUP_WINDOW]
                dup = any(difflib.SequenceMatcher(None, rnorm, t).ratio() >= REPLY_DEDUP_RATIO
                          for (t, a) in recent_replies)
                if dup:
                    _log(out_dir, "skip dup reply")
                else:
                    _say(reply)
                    recent_replies.append((rnorm, now2))
                    _log(out_dir, f"said: {reply[:80]}")

        # End the meeting from the DIALOGUE (no reliance on participantCount):
        # a farewell happened and the humans have gone quiet, or nobody has
        # spoken for a long time at all.
        idle = time.time() - last_human_at
        if farewelled and idle > END_GRACE:
            _log(out_dir, f"end: farewell + {int(idle)}s silence → leaving")
            break
        if MAX_IDLE > 0 and had_human and idle > MAX_IDLE:
            _log(out_dir, f"end: {int(idle)}s no human speech → leaving")
            break

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
