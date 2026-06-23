#!/usr/bin/env python3
"""Post-call summarizer for the Google Meet plugin (reference auto-trigger).

This is the command the bot fires (via ``HERMES_MEET_SUMMARY_CMD``) when a
meeting ends gracefully. It is intentionally LLM-host-agnostic at the contract
level — it reads the meeting's ``transcript.txt`` + ``summary_request.json``,
applies the *meet-post-call-summary* methodology (collapse rolling partials →
content/quality gate → QA-vs-real classification → structured RU report with
tasks/owners/deadlines and "who promised what to whom"), and writes
``report.md`` into the meeting directory.

The LLM step uses the locally-sanctioned ``codex exec`` (codex-only profile, no
OpenAI API key needed): the transcript is piped on stdin, the methodology is the
prompt, and codex's final message is captured straight into ``report.md`` via
``--output-last-message``. read-only sandbox — the CLI writes the report file,
not the model.

Usage:
    meet_summarize.py <meeting-dir>

On success ``report.md`` exists and ``summary_request.json`` flips to
``status: "done"``; on failure it flips to ``status: "failed"`` with an error,
so a watcher can retry without re-summarizing completed meetings.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Caption line: ``[HH:MM:SS] Speaker: text``.
_LINE_RE = re.compile(r"^\[([0-9:]+)\]\s*([^:]+):\s*(.*)$")
# Self-speech is scraped as "You"; relabel so the LLM attributes the bot's turns.
_SELF_LABEL = "Verter (бот)"


def _norm(s: str) -> str:
    """Normalize for partial-comparison: lowercase, strip ALL punctuation,
    collapse whitespace — so 'Привет давай.' / 'привет, давай' / 'Привет давай'
    all compare equal. ASR revises punctuation incrementally as a turn grows, so
    punctuation-sensitive matching would leave near-duplicate partials behind."""
    s = re.sub(r"[^\w\s]", " ", (s or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def _covered(short: str, long: str) -> bool:
    """True if *short* is a (normalized) sub-utterance of *long* — the growth
    stage / scrolled re-capture of the same turn. Requires ≥4 chars to avoid a
    tiny filler ('да') spuriously matching inside an unrelated long turn."""
    ns, nl = _norm(short), _norm(long)
    if not ns or len(ns) < 4:
        return ns == nl
    return ns in nl


def collapse_transcript(raw: str, self_name: str = _SELF_LABEL) -> str:
    """Reconstruct a clean "who said what" dialogue from rolling Meet captions.

    Each transcript line is a full snapshot of the live caption *region*, which
    holds several speaker turns with the **names embedded inline** in the text
    (not as separate lines), re-scraped on every mutation. The region **grows**,
    then **scrolls** (old turns drop out of the ~6 KB buffer) and ASR revises
    earlier words — so a 22-min call is megabytes of overlapping snapshots and
    no single line holds the whole meeting.

    We therefore (1) segment every snapshot into ``(speaker, utterance)`` turns
    by splitting on the participant names, then (2) merge the overlapping
    snapshots at the *turn* level: each incoming turn either extends a recent
    same-speaker turn already kept (growing/ASR-revised → keep the longer) or is
    genuinely new (→ append). The result is one final utterance per spoken turn,
    in order — the markdown dialogue we hand the LLM. ``You`` → *self_name*.
    """
    snaps: list = []
    speakers: set = set()
    for line in raw.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts, lead, text = m.group(1), m.group(2).strip(), m.group(3).strip()
        speakers.add(lead)
        whole = (lead + " " + text) if text else lead
        snaps.append((ts, whole))
    speakers.add("You")
    # Longest names first so "Виталий Бычков" wins over a bare first name.
    names = sorted({s for s in speakers if s}, key=len, reverse=True)
    if not names:
        return ""
    name_re = re.compile("(" + "|".join(re.escape(n) for n in names) + ")")

    def segment(ts: str, whole: str) -> list:
        """Split one region snapshot into [(ts, speaker, utterance)] turns."""
        parts = name_re.split(whole)
        turns = []
        i = 1
        while i < len(parts):
            spk = parts[i].strip()
            utt = parts[i + 1].strip() if i + 1 < len(parts) else ""
            utt = utt.lstrip(":").strip()
            if spk == "You":
                spk = self_name
            if utt:
                turns.append((ts, spk, utt))
            i += 2
        return turns

    # Flatten every snapshot into turns, then globally keep only the MAXIMAL
    # utterance per speaker: the same spoken turn is captured at many growth
    # stages (and reappears across overlapping/scrolled snapshots), so we drop
    # any turn that is a substring of a longer same-speaker turn already kept,
    # and let a longer turn supersede shorter kept prefixes. Order is by first
    # full appearance. This is content-preserving and bounded (the inner loop is
    # over the small set of distinct maximal utterances, not all turns).
    all_turns: list = []
    for ts, whole in snaps:
        all_turns.extend(segment(ts, whole))

    kept: list = []                 # [ts, speaker, text]
    maxs_by_spk: dict = {}          # speaker -> list of kept texts (live refs)
    for ts, spk, utt in all_turns:
        lst = maxs_by_spk.setdefault(spk, [])
        if any(_covered(utt, k) for k in lst):
            continue                # already covered by a longer kept turn
        superseded = [k for k in lst if _covered(k, utt)]
        if superseded:
            sset = set(superseded)
            for k in superseded:
                lst.remove(k)
            kept = [e for e in kept if not (e[1] == spk and e[2] in sset)]
        lst.append(utt)
        kept.append([ts, spk, utt])

    return "\n".join(f"[{ts}] **{spk}:** {txt}" for ts, spk, txt in kept)

# Methodology prompt — mirrors skills/communication/meet-post-call-summary so the
# autonomous path produces the same report shape as the agent-driven one.
_METHODOLOGY = """\
Ты — ассистент, который превращает транскрипт встречи Google Meet (живые,
«растущие» субтитры) в честный пост-отчёт НА РУССКОМ ЯЗЫКЕ. Транскрипт передан
ниже в блоке <stdin>; каждая строка вида `[ЧЧ:ММ:СС] Спикер: текст`.

ШАГ 0 — СХЛОПНИ ПАРТИАЛЫ. Субтитры дедуплицируются только по точной паре
(спикер, текст), поэтому одна фраза идёт несколькими строками-префиксами. Сгруппируй
подряд идущие строки одного спикера; если строка N — префикс/подстрока строки N+1,
оставь только самую длинную (финальную) версию. Суммируй ТОЛЬКО схлопнутые финальные
реплики, никогда не сырые строки.

ШАГ 1 — КОНТЕНТ-ГЕЙТ. Нужно ≥3 содержательных реплик (не «спасибо/угу/ок/алло»).
Если меньше — НЕ выдумывай бизнес-отчёт, выведи одну строку-флаг:
«Встреча <id>: содержательного транскрипта нет (<N> реплик после схлопывания) — отчёт не сформирован.»
Если один спикер доминирует без диалога, это явный тест бота/субтитров/аудио, идёт
филлер-повтор или языковая контаминация (рус + случайные англ/порт/исп фрагменты,
посторонние блоки) — пометь отчёт как «QA / диагностика» и ограничь выводы
операционными наблюдениями (качество захвата, latency, аудио-тракт, дедуп партиалов,
контаминация); НЕ превращай это в бизнес-решения/задачи.

ШАГ 2 — СТРУКТУРА (на русском, markdown):
# Итоги встречи — <тема или meeting-id> (<дата>)
**Участники:** <спикеры из транскрипта>
**Тип:** рабочая встреча   (или: QA / диагностика)

## TL;DR
2–4 предложения: о чём встреча и главный итог.

## Ключевые моменты
- только содержательное, по пунктам

## Решения
- <решение> — <обоснование>  (если нет — «Явных решений не зафиксировано».)

## Задачи (action items)
- [ ] <задача> — **ответственный: <имя из транскрипта>** — срок: <если назван, иначе «не указан»>
(ответственного НЕ выдумывать; неясно — «ответственный: не определён».)

## Договорённости и обещания (кто кому что обещал)
- <кто> → <кому>: <что обещал/взял на себя> — срок: <если назван>
(только явные обещания из транскрипта.)

## Открытые вопросы
- <нерешённое, отложенное, разногласия>

## Дальнейшие шаги / follow-up
- <договорённости о следующих контактах/встречах/проверках>

## Полезная применимая информация
- <конкретные факты, цифры, ссылки на договорённости, что можно сразу применить в работе>

ПРАВИЛА: решения = только явные («договорились/решили/утверждаем»). Пустые секции
допустимы — так и писать, не раздувать. Не добавляй фактов, которых не было в
транскрипте. Шумные/неразборчивые места — суммируй надёжное и явно отметь
неразборчивое, не галлюцинируй. Не вставляй URL встречи, токены, ссылки на записи.

ВЫВОД: верни ТОЛЬКО финальный markdown-отчёт (или одну строку-флаг из ШАГ 1).
Без преамбул, без рассуждений вокруг — только сам отчёт.\
"""


def _codex_bin() -> str:
    """Resolve the codex CLI: explicit env, PATH, or the nvm install."""
    env_bin = os.environ.get("HERMES_MEET_CODEX_BIN", "").strip()
    if env_bin:
        return env_bin
    from shutil import which
    found = which("codex")
    if found:
        return found
    # nvm fallback (codex-only profile installs codex under the active node).
    nvm = Path.home() / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        for ver in sorted(nvm.iterdir(), reverse=True):
            cand = ver / "bin" / "codex"
            if cand.is_file():
                return str(cand)
    return "codex"


def _update_marker(marker: Path, **fields) -> None:
    try:
        data = json.loads(marker.read_text()) if marker.exists() else {}
    except Exception:
        data = {}
    data.update(fields)
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(marker)


def _clean_transcript_text(meeting_dir: Path) -> str:
    """Build a clean ``Speaker: text`` transcript from the bot's live-dedup
    stream (transcript_clean.jsonl), one finalized utterance per line. Returns
    "" when the file is absent/empty so the caller falls back to raw collapse."""
    cp = meeting_dir / "transcript_clean.jsonl"
    if not cp.is_file():
        return ""
    lines = []
    for raw in cp.read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            e = json.loads(raw)
        except Exception:
            continue
        sp = (e.get("speaker") or "").strip() or "Unknown"
        tx = (e.get("text") or "").strip()
        if tx:
            lines.append(f"{sp}: {tx}")
    return "\n".join(lines)


_ACTION_RE = re.compile(
    r"\b(надо|нужно|задач|проверь|проверить|сделать|сделай|подключ|настро|"
    r"исправ|добав|подготов|запиш|разобрать|выясни|todo|fix|check)\b", re.IGNORECASE)


# Banner/noise lines codex prints to stdout that must NOT leak into the report
# header (bug: the whole banner ended up in the "(reason)" parens).
_CODEX_BANNER_RE = re.compile(
    r"^(reading additional input|openai codex v|-{3,}|workdir:|model:|provider:|"
    r"approval:|sandbox:|reasoning( effort| summaries)?:|tokens used:|session id:|"
    r"\[[0-9]{4}-)", re.IGNORECASE)


def _clean_codex_reason(reason: str) -> str:
    """Distil a concise, human-readable failure reason from codex's raw output,
    dropping the startup banner so it never pollutes the report header."""
    r = (reason or "").strip()
    # Prefer the actual API error message if present.
    m = (re.search(r"['\"]message['\"]\s*:\s*['\"]([^'\"]+)['\"]", r)
         or re.search(r"\bmessage\b\s*[:=]\s*(.+)", r))
    if m:
        return m.group(1).strip()[:160]
    low = r.lower()
    if "usage_limit" in low or "usage limit" in low:
        return "достигнут лимит использования codex"
    if re.search(r"\b429\b|rate.?limit|too many requests", low):
        return "codex rate limit (429)"
    # Otherwise take the first line that isn't codex banner noise.
    for line in r.splitlines():
        s = line.strip()
        if s and not _CODEX_BANNER_RE.match(s):
            return s[:160]
    return "ошибка codex (детали в summary_request.json)"


def _extractive_report(meeting_id: str, transcript_text: str, reason: str,
                       rate_limited: bool = False) -> str:
    """LLM-free fallback report so a meeting is NEVER lost when codex is down
    (e.g. 429 usage_limit). Pure extraction from the deduplicated transcript:
    participants, heuristic action-ish lines, and the full turn list. Lower
    quality than the codex summary, but complete and instant — the user can
    regenerate a rich report by re-running summarization once quota resets."""
    lines = [l.strip() for l in transcript_text.splitlines() if l.strip()]
    speakers: list = []
    for l in lines:
        if ":" in l:
            sp = l.split(":", 1)[0].strip()
            if sp and sp.lower() != "you" and sp not in speakers:
                speakers.append(sp)
    actions = [l for l in lines if _ACTION_RE.search(l)][:25]
    # resets_in_seconds is parsed from the RAW reason (it lives in the API JSON);
    # the displayed reason is the cleaned, banner-free version.
    m = re.search(r"resets_in_seconds['\"]?\s*[:=]\s*(\d+)", reason)
    reset_note = (f" Квота восстановится примерно через {int(int(m.group(1)) / 60)} мин — "
                  "тогда можно перегенерировать полноценный отчёт.") if m else ""
    display_reason = ("превышен лимит использования codex (429)"
                      if rate_limited else _clean_codex_reason(reason))
    md = [
        f"# Итоги встречи — {meeting_id}",
        "",
        f"> ⚠️ Экстрактивный фолбэк БЕЗ LLM: авто-суммаризатор (codex) недоступен "
        f"({display_reason}).{reset_note} Ниже — извлечение из транскрипта; для "
        "связного отчёта перезапусти суммаризацию позже.",
        "",
        f"**Участники:** {', '.join(speakers) if speakers else 'не определены'}",
        "",
        "## Возможные задачи / действия (эвристика по ключевым словам)",
    ]
    md += [f"- {l}" for l in actions] or ["- (явных задач не выделено)"]
    md += ["", "## Реплики встречи (дедуплицированные)"]
    md += [f"- {l}" for l in lines[:500]]
    if len(lines) > 500:
        md.append(f"- … ещё {len(lines) - 500} реплик (см. transcript_clean.jsonl)")
    return "\n".join(md) + "\n"


def summarize(meeting_dir: Path) -> int:
    transcript = meeting_dir / "transcript.txt"
    report = meeting_dir / "report.md"
    marker = meeting_dir / "summary_request.json"

    if not transcript.is_file() or not transcript.read_text(encoding="utf-8").strip():
        _update_marker(marker, status="failed", error="no transcript", endedAt=time.time())
        print(f"[meet_summarize] no transcript at {transcript}", file=sys.stderr)
        return 1

    meeting_id = meeting_dir.name
    prompt = _METHODOLOGY.replace("<id>", meeting_id)
    raw = transcript.read_text(encoding="utf-8")
    # Prefer the bot's live-deduplicated stream (transcript_clean.jsonl): it is
    # already one finalized utterance per line, so we skip the lossy rolling-
    # caption collapse. Fall back to collapsing the raw transcript for older
    # runs / when the clean stream is absent or empty.
    clean = _clean_transcript_text(meeting_dir)
    if clean.strip():
        transcript_text = clean
        print(f"[meet_summarize] using clean stream: {len(raw)} raw -> "
              f"{len(transcript_text)} bytes", file=sys.stderr)
    else:
        transcript_text = collapse_transcript(raw)
        if not transcript_text.strip():
            transcript_text = raw  # nothing parsed — fall back to raw
        print(f"[meet_summarize] collapsed {len(raw)} -> {len(transcript_text)} bytes",
              file=sys.stderr)
    # Safety ceiling: keep the TAIL (most recent turns) if still oversized, so a
    # pathological transcript can't overflow the model context.
    max_chars = int(os.environ.get("HERMES_MEET_SUMMARY_MAX_CHARS", "200000"))
    if len(transcript_text) > max_chars:
        transcript_text = transcript_text[-max_chars:]
        print(f"[meet_summarize] truncated to last {max_chars} chars", file=sys.stderr)

    cmd = [
        _codex_bin(), "exec",
        "--skip-git-repo-check",
        "-C", str(meeting_dir),
        "-s", "read-only",
        "--color", "never",
        "--output-last-message", str(report),
        prompt,
    ]
    timeout = float(os.environ.get("HERMES_MEET_SUMMARY_TIMEOUT", "300"))
    try:
        proc = subprocess.run(
            cmd,
            input=transcript_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _update_marker(marker, status="failed", error=f"codex timeout ({timeout}s)",
                       endedAt=time.time())
        print("[meet_summarize] codex exec timed out", file=sys.stderr)
        return 1
    except Exception as e:
        _update_marker(marker, status="failed", error=f"codex spawn: {e}",
                       endedAt=time.time())
        print(f"[meet_summarize] codex spawn failed: {e}", file=sys.stderr)
        return 1

    if proc.returncode != 0 or not report.is_file() or not report.read_text().strip():
        cerr = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        rate_limited = bool(re.search(
            r"usage_limit|rate.?limit|\b429\b|too many requests", cerr, re.IGNORECASE))
        # FALLBACK: never lose the meeting — write an extractive (LLM-free) report.
        # Pass a generous slice (the API JSON with the message/resets_in can be far
        # past the banner) + the rate_limited classification; _extractive_report
        # distils a clean, banner-free reason for the header.
        try:
            report.write_text(
                _extractive_report(meeting_id, transcript_text, cerr[:2000],
                                   rate_limited=rate_limited),
                encoding="utf-8")
            _update_marker(
                marker, status="done_extractive", reportPath=str(report),
                summarizedAt=time.time(),
                error=f"codex rc={proc.returncode} "
                      f"({'rate_limit' if rate_limited else 'error'}); extractive fallback",
            )
            print(f"[meet_summarize] codex failed rc={proc.returncode} "
                  f"({'rate-limit' if rate_limited else 'error'}); wrote EXTRACTIVE "
                  f"fallback to {report}", file=sys.stderr)
            return 0
        except Exception as e:
            _update_marker(
                marker, status="failed",
                error=f"codex rc={proc.returncode}; fallback failed: {e}",
                endedAt=time.time())
            print(f"[meet_summarize] codex failed AND fallback failed: {e}",
                  file=sys.stderr)
            return 1

    _update_marker(marker, status="done", reportPath=str(report), summarizedAt=time.time())
    print(f"[meet_summarize] wrote {report}")
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: meet_summarize.py <meeting-dir>", file=sys.stderr)
        return 2
    meeting_dir = Path(argv[1]).expanduser()
    if not meeting_dir.is_dir():
        print(f"[meet_summarize] not a directory: {meeting_dir}", file=sys.stderr)
        return 2
    return summarize(meeting_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
