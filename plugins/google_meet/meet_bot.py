"""Headless Google Meet bot — Playwright + live-caption scraping.

Runs as a standalone subprocess spawned by ``process_manager.py``. Reads config
from env vars, writes status + transcript to files under
``$HERMES_HOME/workspace/meetings/<meeting-id>/``. The main hermes process
reads those files via the ``meet_*`` tools — no IPC beyond filesystem.

The scraping strategy mirrors OpenUtter (sumansid/openutter): we don't parse
WebRTC audio, we enable Google Meet's built-in live captions and observe the
captions container in the DOM via a MutationObserver. This is lossy and
English-biased but it is:

* deterministic (no API keys, no STT billing),
* works behind Meet's normal login / admission,
* survives Meet UI rewrites fairly well because the caption container has a
  stable ARIA role.

Run standalone for debugging::

    HERMES_MEET_URL=https://meet.google.com/abc-defg-hij \\
    HERMES_MEET_OUT_DIR=/tmp/meet-debug \\
    HERMES_MEET_HEADED=1 \\
    python -m plugins.google_meet.meet_bot

No meet.google.com URL → exits non-zero. Any URL that doesn't start with
``https://meet.google.com/`` is rejected (explicit-by-design).
"""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

# Match ``https://meet.google.com/abc-defg-hij`` or ``.../lookup/...`` — the
# short three-segment code or a lookup URL. Anything else is rejected.
MEET_URL_RE = re.compile(
    r"^https://meet\.google\.com/("
    r"[a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}"
    r"|lookup/[^/?#]+"
    r"|new"
    r")(?:[/?#].*)?$"
)


# Filenames the bot reads/writes in ``HERMES_MEET_OUT_DIR``.
SAY_QUEUE_FILENAME = "say_queue.jsonl"
SAY_PCM_FILENAME = "speaker.pcm"


def _is_safe_meet_url(url: str) -> bool:
    """Return True if *url* is a Google Meet URL we're willing to navigate to."""
    if not isinstance(url, str):
        return False
    return bool(MEET_URL_RE.match(url.strip()))


def _meeting_id_from_url(url: str) -> str:
    """Extract the 3-segment meeting code from a Meet URL.

    For ``https://meet.google.com/abc-defg-hij`` → ``abc-defg-hij``.
    For ``.../lookup/<id>`` or ``/new`` we fall back to a timestamped id — the
    bot won't know the real code until after redirect, and callers pass this
    through to filename anyway.
    """
    m = re.search(
        r"meet\.google\.com/([a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,})",
        url or "",
    )
    if m:
        return m.group(1)
    return f"meet-{int(time.time())}"


# ---------------------------------------------------------------------------
# Status + transcript file writers
# ---------------------------------------------------------------------------

# Live caption dedup (Vexa-inspired). Google Meet emits a SINGLE caption row
# per speaker that GROWS in place ("привет" -> "привет мир" -> ...), plus the
# bot's own TTS gets re-transcribed as echo. The raw transcript.txt keeps every
# growing snapshot (good for the post-call collapse + debugging), but a realtime
# agent polling it drowns in near-duplicate partials. We additionally maintain a
# per-speaker buffer that folds growth/refinement into ONE utterance and emits a
# clean, finalized line to transcript_clean.jsonl when the speaker pauses,
# diverges (new utterance), or the meeting ends. Mirrors Vexa's
# normalize + prefix-confirmation + idle-finalization (see MEET_AGENT_RUNBOOK).
_CAPTION_PUNCT_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_CAPTION_WS_RE = re.compile(r"\s+")
# Seconds a speaker's caption can stay unchanged before we finalize the utterance
# (Vexa idle-finalization; Meet has no is_final flag). Overridable for tests.
_CAPTION_FINALIZE_PAUSE = 2.0


def _caption_norm_words(text: str) -> list[str]:
    return _norm_caption(text).split()


def _merge_caption_tail_overlap(previous: str, current: str) -> Optional[str]:
    """Merge two snapshots when Meet slid the caption window forward.

    Besides pure prefix growth (``A B`` → ``A B C``), Meet can re-render a
    same utterance as an overlapping tail/head window (``A B C D`` → ``C D E``).
    Treat a 2+ word suffix/prefix overlap as one continued utterance and append
    only the new tail, preserving the original text as much as possible.
    """
    prev_words = _caption_norm_words(previous)
    cur_words = _caption_norm_words(current)
    if len(prev_words) < 2 or len(cur_words) < 2:
        return None
    max_k = min(len(prev_words), len(cur_words))
    for k in range(max_k, 1, -1):
        if prev_words[-k:] == cur_words[:k]:
            raw_tail = (current or "").strip().split()[k:]
            if not raw_tail:
                return previous
            return f"{previous.rstrip()} {' '.join(raw_tail)}".strip()
    return None


def _norm_caption(text: str) -> str:
    """Normalize a caption for growth/duplicate comparison ONLY (Vexa-style):
    lowercase, drop punctuation, collapse whitespace. The original text is what
    we store and emit — this is just the comparison key so "привет, мир." and
    "привет мир" fold into one growing utterance."""
    t = (text or "").strip().lower()
    t = _CAPTION_PUNCT_RE.sub(" ", t)
    t = _CAPTION_WS_RE.sub(" ", t)
    return t.strip()


# High-precision closing/farewell markers (RU+EN). Deliberately strict so a
# mid-meeting "спасибо за апдейт" / "thanks for that" does NOT misfire — the
# bot only writes a closing CANDIDATE; the actual graceful exit additionally
# requires a presence-drop (everyone disconnected). See _BotState.
_FAREWELL_RE = re.compile(
    r"(до свидан|до встреч|всем пока|пока пока|увидимся|созвон(имся|]?)?\s*позже|"
    r"встреча (окончен|завершен|законч|подошла к концу)|"
    r"на этом (всё|все|закончим|завершаем|заканчива)|"
    r"(будем|давайте|давай) (заканчива|завершать)|заканчива(ем|ю) (встречу|созвон|совещание)|"
    r"good ?bye|see you|talk (to you )?later|"
    r"that s all for|wrap (it |this )?up|"
    r"end (the )?(call|meeting))",
    flags=re.IGNORECASE,
)


def _is_farewell_candidate(text: str) -> bool:
    """True if an utterance reads like a meeting closing/farewell. High-precision
    candidate only — graceful exit also requires presence-drop (conjunction)."""
    return bool(_FAREWELL_RE.search(_norm_caption(text)))


class _BotState:
    """Single-process mutable state, flushed to ``status.json`` on each change."""

    def __init__(self, out_dir: Path, meeting_id: str, url: str,
                 guest_name: str = ""):
        self.out_dir = out_dir
        self.meeting_id = meeting_id
        self.url = url
        # Our own display name in Meet — used to keep the bot's TTS echo out of
        # the participant roster (Meet attributes our captions to this name).
        self.guest_name = guest_name or ""
        self.in_call = False
        self.captioning = False
        self.captions_enabled_attempted = False
        self.lobby_waiting = False
        self.join_attempted_at: Optional[float] = None
        self.joined_at: Optional[float] = None
        self.last_caption_at: Optional[float] = None
        self.transcript_lines = 0
        self.error: Optional[str] = None
        self.exited = False
        # v2 realtime fields.
        self.realtime = False
        self.realtime_ready = False
        self.realtime_device: Optional[str] = None
        self.audio_bytes_out: int = 0
        self.last_audio_out_at: Optional[float] = None
        self.last_barge_in_at: Optional[float] = None
        self.leave_reason: Optional[str] = None
        # Scraped captions, in order, deduped. Each entry is a dict of
        # {"ts": <epoch>, "speaker": str, "text": str}.
        self._seen: set = set()
        # Live dedup buffers: speaker -> {"text","norm","started","updated"}.
        # Folds growing/refining caption snapshots into one utterance; finalized
        # utterances are appended (clean) to transcript_clean.jsonl.
        self._live: dict = {}
        # Dedup of finalized utterances. `_seen_final` is the set of ALL
        # normalized turns ever emitted per speaker → O(1) exact-match guard that
        # kills re-finalization when Meet scrolls its 2-line caption window and
        # re-renders an OLD row (a 9-min call made 10609 lines for 235 uniques
        # with the previous last-6 window). `_recent_final` (last few) additionally
        # catches partial fragments of a still-recent turn.
        self._seen_final: dict = {}
        self._recent_final: dict = {}
        out_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = out_dir / "transcript.txt"
        self.clean_path = out_dir / "transcript_clean.jsonl"
        self.status_path = out_dir / "status.json"
        # Monotonic id for finalized clean utterances → lossless cursor polling
        # (the agent reads meet_transcript(sinceId=N) and never misses/re-reads
        # a turn). On restart, continue past ids already in the file.
        self._finalize_counter: int = self._next_clean_id()
        # Participant snapshot — drives the live greeting and the presence half
        # of verbal-closure end-detection.
        self.participant_count: Optional[int] = None
        self.participant_names: list = []
        self.greeted: bool = False
        self.admitted_at: Optional[float] = None
        # Last time a human said something that reads like a meeting close. Paired
        # with a presence-drop it triggers a graceful 'verbal_closure' exit.
        self.meeting_closing_at: Optional[float] = None
        self.closing_path = out_dir / "meeting_closing.json"
        self._flush()

    def _next_clean_id(self) -> int:
        """Resume the clean-utterance id counter past whatever is already in
        transcript_clean.jsonl, so a bot relaunch on the same dir never reuses
        an id (which would corrupt the agent's read cursor)."""
        try:
            if not self.clean_path.is_file():
                return 0
            mx = -1
            for ln in self.clean_path.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    i = int(json.loads(ln).get("id", -1))
                    if i > mx:
                        mx = i
                except Exception:
                    continue
            return mx + 1
        except Exception:
            return 0

    # -------- transcript ------------------------------------------------

    def record_caption(self, speaker: str, text: str) -> None:
        """Record a caption snapshot. Appends the raw snapshot to transcript.txt
        (exact-dup guarded, feeds the post-call collapse + debugging) AND folds
        it into the per-speaker live buffer, which emits a clean finalized
        utterance to transcript_clean.jsonl on pause / divergence / end."""
        speaker = (speaker or "").strip() or "Unknown"
        text = (text or "").strip()
        if not text:
            return
        # Live dedup runs on every snapshot (also drives idle-finalization).
        self._update_live(speaker, text)
        key = f"{speaker}|{text}"
        if key in self._seen:
            return
        self._seen.add(key)
        self.transcript_lines += 1
        self.last_caption_at = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(self.last_caption_at))
        line = f"[{ts}] {speaker}: {text}\n"
        # Atomic-ish append — good enough for a single-writer.
        with self.transcript_path.open("a", encoding="utf-8") as f:
            f.write(line)
        self._flush()

    # -------- live caption dedup (Vexa-inspired) -----------------------
    def _update_live(self, speaker: str, text: str) -> None:
        """Fold a growing/refining caption snapshot into the speaker's buffer."""
        now = time.time()
        norm = _norm_caption(text)
        if not norm:
            return
        buf = self._live.get(speaker)
        if buf is None:
            self._live[speaker] = {"text": text, "norm": norm, "started": now, "updated": now}
            return
        bn = buf["norm"]
        if norm == bn:
            return  # identical snapshot — keep the idle timer at last real change
        if norm.startswith(bn) or bn.startswith(norm):
            # Growth or boundary-trim of the SAME utterance: keep longer text.
            if len(norm) >= len(bn):
                buf["text"], buf["norm"] = text, norm
            buf["updated"] = now
            return
        merged = _merge_caption_tail_overlap(buf.get("text", ""), text)
        if merged:
            buf["text"], buf["norm"] = merged, _norm_caption(merged)
            buf["updated"] = now
            return
        # Diverged → new utterance from the same speaker: finalize old, restart.
        self._finalize_speaker(speaker)
        self._live[speaker] = {"text": text, "norm": norm, "started": now, "updated": now}

    def _finalize_speaker(self, speaker: str) -> None:
        buf = self._live.pop(speaker, None)
        if not buf or not (buf.get("text") or "").strip():
            return
        text = buf["text"].strip()
        norm = buf.get("norm") or _norm_caption(text)
        recent = self._recent_final.get(speaker, [])
        seen = self._seen_final.setdefault(speaker, set())
        # ROOT FIX for re-finalization: Meet scrolls its 2-line caption window
        # and re-renders OLD already-closed turns far beyond the recent window —
        # e.g. an old "Вертер, слышишь?" scrolls back into view and gets
        # finalized again with a new id, which then re-triggers the driver's
        # address-gate and makes the bot re-answer ("looping"). Suppress against
        # the set of ALL turns ever emitted for this speaker, not just the last
        # few. (A 9-min call produced 10609 lines for 235 uniques otherwise.)
        if norm in seen:
            return
        for r in recent:
            r_norm = r.get("norm", "") if isinstance(r, dict) else str(r)
            r_id = r.get("id") if isinstance(r, dict) else None
            if norm and norm in r_norm:
                return  # a fragment of a still-recent turn
            if r_norm and r_norm in norm:
                # A late Meet refinement extended an utterance we already
                # finalized. Update that JSONL entry in place (same id/cursor)
                # instead of emitting a duplicate clean turn.
                if isinstance(r_id, int) and self._rewrite_clean_entry(r_id, text):
                    r["norm"] = norm
                    r["text"] = text
                    seen.add(norm)
                return
        entry = {
            "id": self._finalize_counter,
            "ts": time.strftime("%H:%M:%S", time.localtime(buf["started"])),
            "speaker": speaker,
            "text": text,
        }
        self._finalize_counter += 1
        # Remember this finalized turn: full set for exact-match (scroll re-render)
        # + a short recent list for prefix/fragment folding. Cap the set so a
        # multi-hour call can't grow it without bound.
        seen.add(norm)
        if len(seen) > 3000:
            seen.clear()
        recent.append({"norm": norm, "id": entry["id"], "text": text})
        self._recent_final[speaker] = recent[-8:]
        is_human = _looks_like_human_speaker(speaker, getattr(self, "guest_name", ""))
        # Track human speakers for greeting / presence (best-effort; the People
        # panel is unreliable, but whoever actually spoke is a real participant).
        if is_human and speaker not in self.participant_names:
            self.participant_names.append(speaker)
        # Closing candidate: a human said something that reads like a farewell.
        # Recorded only; the actual graceful exit needs presence-drop too.
        if is_human and _is_farewell_candidate(text):
            self.meeting_closing_at = time.time()
            try:
                self.closing_path.write_text(json.dumps(
                    {"at": self.meeting_closing_at, "by": speaker, "quote": text[:200]},
                    ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
        try:
            with self.clean_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _rewrite_clean_entry(self, entry_id: int, text: str) -> bool:
        """Replace a finalized clean entry with a late longer refinement.

        Keeps the same monotonic id so cursor-based readers do not see a second
        duplicate turn, while post-call summarization receives the most complete
        text available.
        """
        try:
            if not self.clean_path.is_file():
                return False
            rows = []
            changed = False
            for raw in self.clean_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    rows.append(raw)
                    continue
                if int(item.get("id", -1)) == entry_id:
                    item["text"] = text
                    changed = True
                rows.append(json.dumps(item, ensure_ascii=False))
            if not changed:
                return False
            tmp = self.clean_path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(rows) + "\n", encoding="utf-8")
            tmp.replace(self.clean_path)
            return True
        except Exception:
            return False

    def tick_finalize(self, now: Optional[float] = None) -> None:
        """Finalize any speaker whose caption has been static for the pause
        window (Vexa idle-finalization — Meet has no is_final flag). Call this
        periodically from the main loop so utterances close on natural pauses."""
        ref = now if now is not None else time.time()
        for speaker in list(self._live.keys()):
            if ref - self._live[speaker]["updated"] >= _CAPTION_FINALIZE_PAUSE:
                self._finalize_speaker(speaker)

    def finalize_all(self) -> None:
        """Flush all in-progress utterances (call at meeting teardown)."""
        for speaker in list(self._live.keys()):
            self._finalize_speaker(speaker)

    # -------- status file ----------------------------------------------

    def _flush(self) -> None:
        data = {
            "meetingId": self.meeting_id,
            "url": self.url,
            "inCall": self.in_call,
            "captioning": self.captioning,
            "captionsEnabledAttempted": self.captions_enabled_attempted,
            "lobbyWaiting": self.lobby_waiting,
            "joinAttemptedAt": self.join_attempted_at,
            "joinedAt": self.joined_at,
            "lastCaptionAt": self.last_caption_at,
            "transcriptLines": self.transcript_lines,
            "transcriptPath": str(self.transcript_path),
            "error": self.error,
            "exited": self.exited,
            "pid": os.getpid(),
            # v2 realtime telemetry.
            "realtime": self.realtime,
            "realtimeReady": self.realtime_ready,
            "realtimeDevice": self.realtime_device,
            "audioBytesOut": self.audio_bytes_out,
            "lastAudioOutAt": self.last_audio_out_at,
            "lastBargeInAt": self.last_barge_in_at,
            "leaveReason": self.leave_reason,
            # Lossless-polling cursor: highest finalized clean-utterance id so
            # far (-1 = none yet). Agent polls meet_transcript(sinceId=...).
            "transcriptCleanLastId": self._finalize_counter - 1,
            # Participant snapshot for greeting + presence end-detection.
            "participantCount": self.participant_count,
            "participantNames": list(self.participant_names),
            "greeted": self.greeted,
            "admittedAt": self.admitted_at,
        }
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def set(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self._flush()

    # -------- end-of-meeting summary signal ----------------------------

    def request_summary(self) -> Optional["Path"]:
        """Drop a ``summary_request.json`` marker in the meeting dir.

        Written only when the meeting was actually attended and has content
        (so denied/lobby-timeout never request a summary). This file is the
        contract a post-call summarizer watches for: the bot never runs the
        LLM itself (it has no model/key) — an external hook or the Hermes
        agent picks up the marker and produces ``report.md``. Returns the
        marker path, or None if there was nothing to summarize.
        """
        if self.joined_at is None or self.transcript_lines <= 0:
            return None
        marker = self.out_dir / "summary_request.json"
        payload = {
            "meetingId": self.meeting_id,
            "url": self.url,
            "transcriptPath": str(self.transcript_path),
            "transcriptLines": self.transcript_lines,
            "leaveReason": self.leave_reason,
            "endedAt": time.time(),
            "reportPath": str(self.out_dir / "report.md"),
            "status": "pending",
        }
        tmp = marker.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(marker)
        return marker


# ---------------------------------------------------------------------------
# Playwright bot entry point
# ---------------------------------------------------------------------------

# JavaScript injected into the Meet tab to observe captions. Captures
# {speaker, text} tuples via a MutationObserver on the caption container,
# and exposes ``window.__hermesMeetDrain()`` to pull new entries. This
# mirrors the OpenUtter caption scraping approach.
_CAPTION_OBSERVER_JS = r"""
(() => {
  if (window.__hermesMeetInstalled) return;
  window.__hermesMeetInstalled = true;
  window.__hermesMeetQueue = [];

  // The caption region's DOM node is recreated by Meet (when captions toggle,
  // settings open/close, or speech starts), which orphans a MutationObserver
  // bound to a single node. So we observe document.body and RE-QUERY the
  // current caption region on every mutation — robust against node swaps.
  const captionSelector = '[role="region"][aria-label*="aption" i], ' +
                          'div[jsname="dsyhDe"]';  // current (Jun 2026, verified)

  let lastKey = '';
  function pushEntry(speaker, text) {
    if (!text || !text.trim()) return;
    const key = (speaker || '') + '\x1f' + text.trim();
    if (key === lastKey) return;  // collapse consecutive identical scans
    lastKey = key;
    window.__hermesMeetQueue.push({
      ts: Date.now(),
      speaker: (speaker || '').trim(),
      text: text.trim(),
    });
  }

  function scan() {
    const root = document.querySelector(captionSelector);
    if (!root) return;
    // PER-SPEAKER rows. CRITICAL: the region node itself carries
    // jsname="dsyhDe", so the old `querySelectorAll('div[jsname="dsyhDe"]')`
    // matched the WHOLE region as one "row" and collapsed every speaker into a
    // single blob (verified via caption_dom dump, 2026-06-22). The real rows are
    // `.nMcdL` (one per speaker turn) with the speaker name in `.KcIKyf`. Class
    // names drift across Meet rewrites, so we try rows first, then structural
    // children, then fall back to the whole-region line split.
    let rows = root.querySelectorAll('.nMcdL');
    if (!rows.length) rows = root.querySelectorAll(':scope > div > div');
    const targets = (rows && rows.length) ? rows : [root];
    targets.forEach((row) => {
      // Speaker name is its own node (class drifts → try a few); strip it from
      // the row text to get the spoken words.
      let speaker = '';
      const sp = row.querySelector('.KcIKyf, .zs7s8d, [data-self-name]');
      if (sp) speaker = (sp.innerText || '').trim();
      const lines = (row.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
      if (!lines.length) return;
      if (speaker) {
        // Drop the leading speaker-name line(s), keep the rest as text.
        let rest = lines.filter((l) => l !== speaker);
        const text = rest.join(' ').trim();
        if (text) pushEntry(speaker, text);
      } else if (lines.length === 1) {
        pushEntry('', lines[0]);
      } else {
        pushEntry(lines[0], lines.slice(1).join(' '));
      }
    });
  }

  const obs = new MutationObserver(() => scan());
  obs.observe(document.body, { childList: true, subtree: true, characterData: true });
  scan();

  window.__hermesMeetDrain = () => {
    const out = window.__hermesMeetQueue.slice();
    window.__hermesMeetQueue = [];
    return out;
  };
})();
"""


def _enable_captions_js() -> str:
    """Return JS that turns on Meet's live captions.

    Locale-independent + state-aware. Primary anchor is the material-icon
    ligature ``closed_caption_off`` (captions currently OFF) which is identical
    in every UI language; falls back to text ("Turn on captions" / RU "Включить
    субтитры") then the ``c`` keystroke. Idempotent: only clicks when captions
    are OFF — once ON the button shows ``closed_caption`` / "Turn off", which is
    left alone, so re-running never toggles captions back off. The button only
    exists *in-call*, so this is a no-op in the lobby. Returns true if it
    clicked. The synthetic ``c`` keystroke alone does not work headless.
    """
    return r"""
    (() => {
      const buttons = Array.from(document.querySelectorAll('button'));
      const off = buttons.find((b) => {
        const inner = b.innerText || '';
        const s = inner + ' ' + (b.getAttribute('aria-label') || '');
        return /closed_caption_off/i.test(inner)
          || /turn on captions|включить субтитры/i.test(s);
      });
      if (off) { off.click(); return true; }
      // Already on? (ligature 'closed_caption' without _off, or "turn off")
      const on = buttons.some((b) => {
        const inner = b.innerText || '';
        const s = inner + ' ' + (b.getAttribute('aria-label') || '');
        return /closed_caption(?!_off)/i.test(inner)
          || /turn off captions|выключить субтитры|отключить субтитры/i.test(s);
      });
      if (on) return false;
      const ev = new KeyboardEvent('keydown', {
        key: 'c', code: 'KeyC', keyCode: 67, which: 67, bubbles: true,
      });
      document.body.dispatchEvent(ev);
      return false;
    })();
    """


def _set_caption_language(page, lang: str) -> bool:
    """Open caption settings and set the meeting/caption language.

    Robust to the settings UI being in RU or EN. Meet transcribes ONLY the
    configured language (no auto-detect), so transcribe mode must set it or the
    caption region stays empty. Opens Settings → Captions (caption-settings text
    in RU/EN, else the ``settings`` gear ligature), opens the "Language of the
    meeting" combobox (ARIA role, preferring one whose name mentions
    language/язык when several exist), and selects the Russian option by its
    localized label — "Russian" in EN UI, "Русский" in RU UI — matched
    independently of the configured value so captions always end up Russian. The
    configured *lang* is re.escaped and added as an extra alias. Best-effort;
    returns True only when an option was selected. Closes via the dialog's close
    control (Escape fallback) so it doesn't cover the caption region.
    """
    def _log(msg: str) -> None:
        try:
            print(f"[meet_bot] caption-language: {msg}", flush=True)
        except Exception:
            pass

    # The language picker lives ONLY in the caption-settings dialog reached via
    # the caption overlay's "Open caption settings" button — that entry opens
    # Settings directly on the Captions section with the "Language of the
    # meeting" combobox. The generic ⋮ → Settings dialog has NO Captions tab, so
    # it is a dead end. The button appears a beat after captions are enabled, so
    # retry briefly (RU/EN text).
    opened_settings = False
    for _ in range(6):
        try:
            opened_settings = bool(page.evaluate(
                r"""()=>{const bs=[...document.querySelectorAll('button,[role="button"]')];
                  const t=bs.find(x=>/open caption settings|(настройки|параметры) субтитров/i
                    .test((x.innerText||'')+' '+(x.getAttribute('aria-label')||'')));
                  if(t){t.click();return true;} return false;}"""
            ))
        except Exception:
            opened_settings = False
        if opened_settings:
            break
        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass
    if not opened_settings:
        _log("'open caption settings' not available (captions overlay not up yet)")
        return False
    try:
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # Always select Russian; also accept the configured label (escaped) as alias.
    safe = re.escape((lang or "").strip())
    ru_pat = r"^\s*(russian|русск" + (("|" + safe) if safe else "") + r")"
    rx_ru = re.compile(ru_pat, re.I)

    # Open the language combobox (role anchor; prefer a language/язык-named one).
    # The dialog renders asynchronously, so poll a few times (count() is instant)
    # before giving up rather than checking once too early.
    opened = False
    for _ in range(5):
        try:
            named = page.get_by_role("combobox", name=re.compile(r"language|язык", re.I))
            if named.count():
                named.first.click(timeout=2_000)
                opened = True
                break
            any_cb = page.get_by_role("combobox")
            if any_cb.count():
                any_cb.first.click(timeout=2_000)
                opened = True
                break
        except Exception as e:
            _log(f"combobox open error: {e}")
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
    if not opened:
        _log("language combobox not found (settings/Captions tab not open)")
        return False
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass

    selected = False
    try:
        opt = page.get_by_role("option", name=rx_ru).first
        if opt.count():
            opt.click(timeout=3_000)
            selected = True
    except Exception as e:
        _log(f"option select failed: {e}")
        selected = False
    if not selected:
        _log("russian option not found")

    # Close settings via its close control; Escape only as a fallback.
    try:
        closed = page.evaluate(
            r"""()=>{
              const b=[...document.querySelectorAll('button,[role="button"]')].find(x=>
                (x.innerText||'').trim()==='close'
                || /^(close|закрыть)$/i.test(x.getAttribute('aria-label')||''));
              if(b){b.click();return true;} return false;
            }"""
        )
    except Exception:
        closed = False
    if not closed:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    return selected


def _start_realtime_speaker(
    *,
    rt: dict,
    out_dir: Path,
    bridge_info: dict,
    api_key: str,
    model: str,
    voice: str,
    instructions: str,
    stop_flag: dict,
    state: "_BotState",
) -> None:
    """Wire up the OpenAI Realtime session + speaker thread + PCM pump.

    The speaker thread reads text lines from ``say_queue.jsonl``, sends each
    to OpenAI Realtime, and writes PCM audio into ``speaker.pcm``. A
    separate *pump* thread forwards that PCM into the OS audio sink so
    Chrome's fake mic picks it up. On Linux we pipe to ``paplay`` against
    the null-sink; on macOS the caller is expected to have the BlackHole
    device selected as default input.
    """
    try:
        from plugins.google_meet.realtime.openai_client import (
            RealtimeSession,
            RealtimeSpeaker,
        )
    except Exception as e:
        state.set(error=f"realtime import failed: {e}")
        return

    # TTS backend selection. Default = Silero (self-hosted, open-weights local voice).
    # Raw PCM, no transcode — same downstream pump/sink/fake-mic chain.
    # Set HERMES_MEET_TTS=openai to use OpenAI Realtime instead.
    tts_backend = os.environ.get("HERMES_MEET_TTS", "").strip().lower()

    pcm_path = out_dir / SAY_PCM_FILENAME
    queue_path = out_dir / SAY_QUEUE_FILENAME
    processed_path = out_dir / "say_processed.jsonl"
    # Reset the sink file so we start clean each session.
    pcm_path.write_bytes(b"")
    # Make sure the queue exists so the speaker poller doesn't error on
    # first iteration.
    queue_path.touch()

    try:
        if tts_backend == "silero":
            from plugins.google_meet.realtime.silero_client import SileroSpeaker
            silero_voice = os.environ.get("HERMES_MEET_SILERO_VOICE", "").strip() or None
            _sr = os.environ.get("HERMES_MEET_SILERO_RATE", "").strip()
            session = SileroSpeaker(
                audio_sink_path=pcm_path,
                voice=silero_voice,
                sample_rate=int(_sr) if _sr else 48000,
            )
        else:
            session = RealtimeSession(
                api_key=api_key,
                model=model,
                voice=voice,
                instructions=instructions,
                audio_sink_path=pcm_path,
                sample_rate=24000,
            )
        session.connect()
    except Exception as e:
        state.set(error=f"realtime connect failed ({tts_backend or 'openai_realtime'}): {e}")
        return

    rt["session"] = session

    def _stop_fn():
        return stop_flag.get("stop", False)

    rt["speaker_stop"] = lambda: stop_flag.__setitem__("stop", stop_flag.get("stop", False))

    speaker = RealtimeSpeaker(
        session=session,
        queue_path=queue_path,
        processed_path=processed_path,
    )

    def _speaker_loop():
        try:
            speaker.run_until_stopped(_stop_fn)
        except Exception as e:
            state.set(error=f"realtime speaker crashed: {e}")

    t_speaker = threading.Thread(target=_speaker_loop, name="meet-speaker", daemon=True)
    t_speaker.start()
    rt["speaker_thread"] = t_speaker

    # PCM pump: feeds speaker.pcm (24kHz s16le mono) into the OS audio
    # device that Chrome's fake mic reads from. Different tools per
    # platform, but the contract is the same — block-read the growing
    # PCM file and stream it to the device in near-real-time.
    platform_tag = (bridge_info or {}).get("platform")
    if platform_tag == "linux":
        import subprocess as _sp

        sink = (bridge_info or {}).get("write_target") or "hermes_meet_sink"
        rate = getattr(session, "sample_rate", 24000)
        try:
            # paplay reads stdin (no file arg) and a tailer thread streams the
            # GROWING speaker.pcm into it. Passing the file path directly would
            # make paplay read to EOF once and exit — but speaker.pcm starts
            # empty and is appended to as TTS synthesizes, so paplay would quit
            # before any audio existed (silent call). Tailing keeps it live.
            proc = _sp.Popen(
                [
                    "paplay",
                    "--raw",
                    f"--rate={rate}",
                    "--format=s16le",
                    "--channels=1",
                    f"--device={sink}",
                    # Buffer ~250ms so brief gaps between the tailer's PCM writes
                    # (sentence boundaries / EOF polls) don't underrun the sink
                    # and cause occasional audible stutter. Cheap latency for
                    # smooth playback; far less than the Meet caption lag anyway.
                    "--latency-msec=250",
                ],
                stdin=_sp.PIPE,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            rt["pcm_pump"] = proc

            def _pcm_tailer():
                try:
                    with open(pcm_path, "rb") as fh:
                        while not stop_flag.get("stop"):
                            chunk = fh.read(4096)
                            if chunk:
                                try:
                                    proc.stdin.write(chunk)
                                    proc.stdin.flush()
                                except Exception:
                                    break
                            else:
                                time.sleep(0.05)
                except Exception:
                    pass
                try:
                    proc.stdin.close()
                except Exception:
                    pass

            _tt = threading.Thread(target=_pcm_tailer, name="meet-pcm-tail", daemon=True)
            _tt.start()
            rt["pcm_tail"] = _tt
        except FileNotFoundError:
            state.set(error="paplay not found — install pulseaudio-utils for realtime on Linux")
    elif platform_tag == "darwin":
        # macOS: use ffmpeg to tail-read speaker.pcm and write it to the
        # BlackHole output device. The user must have BlackHole selected
        # as the default input in System Settings → Sound for Chrome to
        # pick it up. We prefer ffmpeg because it's scriptable and can
        # target AVFoundation devices by name; fall back to afplay-ing
        # the file in a tight loop if ffmpeg is absent.
        import shutil as _shutil
        import subprocess as _sp

        device_name = (bridge_info or {}).get("write_target") or "BlackHole 2ch"
        if _shutil.which("ffmpeg"):
            try:
                # -re: read input at native frame rate.
                # -f avfoundation -i: speaker path as raw PCM.
                # -f s16le -ar 24000 -ac 1 -i <pcm>: interpret the file.
                # -f audiotoolbox -audio_device_index: write to BlackHole.
                # Simpler: output as raw via coreaudio using "-f audiotoolbox".
                # ffmpeg's audiotoolbox output picks the current default
                # output device, which isn't what we want. Instead we use
                # -f avfoundation with the named device as OUTPUT via
                # -vn and the device name.
                proc = _sp.Popen(
                    [
                        "ffmpeg",
                        "-nostdin", "-hide_banner", "-loglevel", "error",
                        "-re",
                        "-f", "s16le", "-ar", str(getattr(session, 'sample_rate', 24000)), "-ac", "1",
                        "-i", str(pcm_path),
                        "-f", "audiotoolbox",
                        "-audio_device_index", _mac_audio_device_index(device_name),
                        "-",
                    ],
                    stdin=_sp.DEVNULL,
                    stdout=_sp.DEVNULL,
                    stderr=_sp.DEVNULL,
                )
                rt["pcm_pump"] = proc
            except FileNotFoundError:
                state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")
            except Exception as e:
                state.set(error=f"macOS pcm pump failed to start: {e}")
        else:
            state.set(error="ffmpeg not found — install via `brew install ffmpeg` for realtime on macOS")


def _mac_audio_device_index(device_name: str) -> str:
    """Return the ffmpeg ``-audio_device_index`` for *device_name*, as a string.

    Probes ``ffmpeg -f avfoundation -list_devices true -i ''`` (which prints
    the device table on stderr) and matches *device_name* case-insensitively.
    Defaults to ``"0"`` if the device can't be found — caller will get a
    misrouted stream but not a crash, and the error will be obvious.
    """
    import subprocess as _sp

    try:
        out = _sp.run(
            ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return "0"
    # ffmpeg prints the table on stderr. Lines look like:
    #   [AVFoundation indev @ 0x...] [0] BlackHole 2ch
    import re as _re

    needle = device_name.strip().lower()
    for line in (out.stderr or "").splitlines():
        m = _re.search(r"\[(\d+)\]\s+(.+)$", line)
        if not m:
            continue
        if m.group(2).strip().lower() == needle:
            return m.group(1)
    return "0"


def run_bot() -> int:  # noqa: C901 — orchestration, explicit branches
    url = os.environ.get("HERMES_MEET_URL", "").strip()
    out_dir_env = os.environ.get("HERMES_MEET_OUT_DIR", "").strip()
    headed = os.environ.get("HERMES_MEET_HEADED", "").lower() in {"1", "true", "yes"}
    auth_state = os.environ.get("HERMES_MEET_AUTH_STATE", "").strip()
    guest_name = os.environ.get("HERMES_MEET_GUEST_NAME", "Hermes Agent")
    duration_s = _parse_duration(os.environ.get("HERMES_MEET_DURATION", ""))
    # v2: optional realtime mode. Enabled when HERMES_MEET_MODE=realtime.
    mode = os.environ.get("HERMES_MEET_MODE", "transcribe").strip().lower()
    realtime_model = os.environ.get("HERMES_MEET_REALTIME_MODEL", "gpt-realtime")
    realtime_voice = os.environ.get("HERMES_MEET_REALTIME_VOICE", "alloy")
    realtime_instructions = os.environ.get("HERMES_MEET_REALTIME_INSTRUCTIONS", "")
    realtime_api_key = os.environ.get("HERMES_MEET_REALTIME_KEY") or os.environ.get("OPENAI_API_KEY", "")

    if not url or not _is_safe_meet_url(url):
        sys.stderr.write(
            "google_meet bot: refusing to launch — HERMES_MEET_URL must be a "
            "meet.google.com URL. got: %r\n" % url
        )
        return 2
    if not out_dir_env:
        sys.stderr.write("google_meet bot: HERMES_MEET_OUT_DIR is required\n")
        return 2

    out_dir = Path(out_dir_env)
    meeting_id = _meeting_id_from_url(url)
    state = _BotState(out_dir=out_dir, meeting_id=meeting_id, url=url,
                      guest_name=guest_name)

    # SIGTERM → exit cleanly so the parent ``meet_leave`` gets a finalized
    # transcript. We set a flag instead of raising so the Playwright context
    # teardown runs in the finally block below.
    stop_flag = {"stop": False}

    def _on_signal(_sig, _frame):
        stop_flag["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # v2 realtime: provision virtual audio device + start speaker thread.
    # We track these in a dict so the finally block can tear them down
    # regardless of how we exit. If anything in the realtime setup fails we
    # fall back to transcribe mode with a status flag.
    rt = {
        "enabled": mode == "realtime",
        "bridge": None,            # AudioBridge | None
        "bridge_info": None,       # dict | None
        "session": None,           # RealtimeSession | None
        "speaker_thread": None,    # threading.Thread | None
        "speaker_stop": None,      # callable | None
    }
    # The bot uses Silero TTS for realtime voice synthesis (no API key needed).
    # Set HERMES_MEET_TTS=openai to switch to OpenAI Realtime (requires API key).
    _tts_backend = os.environ.get("HERMES_MEET_TTS", "").strip().lower()
    if rt["enabled"]:
        if _tts_backend == "openai" and not realtime_api_key:
            state.set(error="OpenAI realtime TTS requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe mode")
            rt["enabled"] = False
        else:
            try:
                from plugins.google_meet.audio_bridge import AudioBridge
                bridge = AudioBridge()
                rt["bridge_info"] = bridge.setup()
                rt["bridge"] = bridge
                state.set(realtime=True, realtime_device=rt["bridge_info"].get("device_name"))
            except Exception as e:
                state.set(error=f"audio bridge setup failed: {e} — falling back to transcribe")
                rt["enabled"] = False

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        state.set(error=f"playwright not installed: {e}", exited=True)
        sys.stderr.write(
            "google_meet bot: playwright is not installed. Run "
            "`pip install playwright && python -m playwright install chromium`\n"
        )
        if rt["bridge"]:
            rt["bridge"].teardown()
        return 3

    # Chrome env: only realtime mode needs an input device. Transcribe mode is
    # receive-only and must not publish Chromium's fake microphone tone into Meet.
    chrome_env = os.environ.copy()
    chrome_args = ["--disable-blink-features=AutomationControlled"]
    if rt["enabled"]:
        chrome_args.insert(0, "--use-fake-ui-for-media-stream")
        if rt["bridge_info"] and rt["bridge_info"].get("platform") == "linux":
            chrome_env["PULSE_SOURCE"] = rt["bridge_info"].get("device_name", "")
    # Egress proxy (env-gated). The Google session must be minted *and* used
    # from the same network region — mixing RU-direct and DE-proxy exits gets
    # the session invalidated server-side. Route Chrome through the DE proxy
    # when HERMES_MEET_PROXY is set; bypass loopback so local IPC is direct.
    meet_proxy = os.environ.get("HERMES_MEET_PROXY", "").strip()
    if meet_proxy:
        chrome_args.append(f"--proxy-server={meet_proxy}")
        chrome_args.append("--proxy-bypass-list=127.0.0.1;localhost;[::1]")
    # UI language (env-gated). HERMES_MEET_LANG=ru-RU forces Chrome's UI +
    # Accept-Language so Meet renders Russian — exercises the RU button/label
    # matchers. Unset => the profile's native locale (no behavior change).
    meet_lang = os.environ.get("HERMES_MEET_LANG", "").strip()
    if meet_lang:
        chrome_args.append(f"--lang={meet_lang}")
    # Real-Chrome on a headless server typically needs the sandbox disabled
    # (no user namespace). Gate it so the default bundled-Chromium path is
    # untouched.
    if os.environ.get("HERMES_MEET_NO_SANDBOX", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        chrome_args.append("--no-sandbox")

    try:
        with sync_playwright() as pw:
            # Playwright's launch() doesn't take env; we set PULSE_SOURCE
            # via the process env before launch so the child Chrome inherits it.
            for k, v in chrome_env.items():
                os.environ[k] = v
            # Browser engine selection (env-gated; default = bundled Chromium,
            # unchanged behavior). Real Chrome + a persistent profile is the
            # robust path against Google's "this browser may not be secure"
            # automation block. executable_path and channel are mutually
            # exclusive in Playwright, so prefer an explicit path when given.
            chrome_channel = os.environ.get("HERMES_MEET_CHROME_CHANNEL", "").strip()
            chrome_path = os.environ.get("HERMES_MEET_CHROME_PATH", "").strip()
            user_data_dir = os.environ.get("HERMES_MEET_USER_DATA_DIR", "").strip()
            launch_kwargs = {"headless": not headed, "args": chrome_args}
            if chrome_path:
                launch_kwargs["executable_path"] = chrome_path
            elif chrome_channel:
                launch_kwargs["channel"] = chrome_channel
            context_args = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
            }
            if rt["enabled"]:
                context_args["permissions"] = ["microphone", "camera"]
            if meet_lang:
                context_args["locale"] = meet_lang
            if user_data_dir:
                # Self-heal: drop a stale lock from a crashed prior bot so we
                # take the REAL signed-in profile (not a guest fallback that
                # Meet would bounce as uninvited).
                _clear_stale_singleton_lock(user_data_dir)
                # Persistent real-Chrome profile: the signed-in Google session
                # lives in the profile itself, so no storage_state is needed.
                # Keep Chrome's *native* user-agent — overriding it to a fake
                # Chrome/124 string after signing in with the real UA triggers
                # Google's security re-check and invalidates the session.
                context_args.pop("user_agent", None)
                browser = None
                context = pw.chromium.launch_persistent_context(
                    user_data_dir, **launch_kwargs, **context_args
                )
                page = context.pages[0] if context.pages else context.new_page()
            else:
                if auth_state and Path(auth_state).is_file():
                    context_args["storage_state"] = auth_state
                browser = pw.chromium.launch(**launch_kwargs)
                context = browser.new_context(**context_args)
                page = context.new_page()

            # Optional auth gate (reference authed flow). Off by default so the
            # guest / storage_state smoke paths are unaffected. When required,
            # fail fast with a clear reason instead of silently landing on
            # Google's "browser may not be secure" page mid-join.
            if os.environ.get("HERMES_MEET_REQUIRE_AUTH", "").strip().lower() in (
                "1",
                "true",
                "yes",
            ):
                authed, reason = _auth_gate(page)
                if not authed:
                    state.set(error=f"auth gate failed: {reason}", exited=True)
                    return 4

            # Realtime: force mic capture constraints before Meet's JS runs, so
            # Chrome/Meet don't apply echo-cancellation / noise-suppression /
            # auto-gain that suppress our steady loopback (virtual-mic) audio as
            # "noise". Best-effort: Chrome may keep some processing, but this
            # removes the constraint-driven suppression of synthetic speech.
            if mode == "realtime":
                try:
                    page.add_init_script(
                        """(() => {
                          const md = navigator.mediaDevices;
                          if (!md || !md.getUserMedia) return;
                          const orig = md.getUserMedia.bind(md);
                          md.getUserMedia = (c) => {
                            c = c || {};
                            if (c.audio) {
                              const a = (typeof c.audio === 'object') ? c.audio : {};
                              c.audio = Object.assign({}, a, {
                                echoCancellation: false,
                                noiseSuppression: false,
                                autoGainControl: false,
                              });
                            }
                            return orig(c);
                          };
                        })();"""
                    )
                except Exception:
                    pass

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:
                state.set(error=f"navigate failed: {e}", exited=True)
                return 4

            # Guest-mode: Meet shows a name field before "Ask to join". When
            # we're authed, we instead see "Join now".
            _try_guest_name(page, guest_name)
            join_clicked = _click_join(page, state)
            if not join_clicked:
                # Always-on, file-based diagnostics (safe in a detached bot).
                _dump_admission_snapshot(page, out_dir, "join button not clicked")
                if os.environ.get('HERMES_MEET_DEBUG_MODE', '').lower() in ('1', 'true', 'yes'):
                    try:
                        debug = page.evaluate(
                            r"""
                            () => ({
                              url: location.href,
                              text: (document.body && document.body.innerText || '').slice(0, 2000),
                              buttons: Array.from(document.querySelectorAll('button')).map((b, i) => ({
                                i,
                                inner: (b.innerText || '').trim(),
                                aria: (b.getAttribute('aria-label') || '').trim(),
                                visible: !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length),
                                disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
                              })).filter((x) => x.visible),
                            })
                            """
                        )
                        print(f"[meet_bot] join button not clicked; continuing admission loop debug={debug!r}", flush=True)
                    except Exception as e:
                        print(f"[meet_bot] join button debug failed: {e!r}", flush=True)

            # Install the caption observer now — it retries on an interval
            # until the caption region appears. Enabling captions is DEFERRED
            # until after admission (the "Turn on captions" button only exists
            # in-call); doing it here, in the lobby, was a silent no-op and the
            # root cause of empty transcripts.
            try:
                page.evaluate(_CAPTION_OBSERVER_JS)
            except Exception as e:
                state.set(error=f"caption observer install failed: {e}")

            # Note: in_call=False until admission is confirmed (we detect
            # either the Leave button or the caption region, signalling we
            # made it past the lobby).
            state.set(captioning=True, join_attempted_at=time.time())

            # v2 realtime: start the speaker thread reading from the
            # plugin-side say queue. The thread reads JSONL lines written by
            # meet_say, calls OpenAI Realtime, and streams the audio PCM to
            # the virtual sink that Chrome's fake-mic is pointed at.
            if rt["enabled"]:
                _start_realtime_speaker(
                    rt=rt,
                    out_dir=out_dir,
                    bridge_info=rt["bridge_info"],
                    api_key=realtime_api_key,
                    model=realtime_model,
                    voice=realtime_voice,
                    instructions=realtime_instructions,
                    stop_flag=stop_flag,
                    state=state,
                )
                if rt["session"] is not None:
                    state.set(realtime_ready=True)

            # Admission + drain loop. Runs until SIGTERM, duration expiry,
            # or the page detects "You were removed / you left the
            # meeting". Responsible for:
            #   * detecting admission (Leave button visible → in_call=True)
            #   * timing out stuck-in-lobby (default 5 minutes)
            #   * draining scraped captions into the transcript
            #   * triggering realtime barge-in when a human speaks while
            #     the bot is generating audio
            #   * periodically flushing realtime counters into status.json
            deadline = (time.time() + duration_s) if duration_s else None
            lobby_deadline = time.time() + float(
                os.environ.get("HERMES_MEET_LOBBY_TIMEOUT", "300")
            )
            last_admission_check = 0.0
            last_caption_enable = 0.0
            caption_lang = os.environ.get("HERMES_MEET_CAPTION_LANG", "").strip()
            caption_lang_done = False
            last_lang_try = 0.0
            lang_attempts = 0
            mic_unmute_attempts = 0
            # Auto-end on empty meeting. Only arm AFTER the bot has seen company
            # (≥2 participants) at least once — otherwise a bot that joins before
            # the host would immediately "leave alone" in an empty room. Once
            # everyone else has gone for HERMES_MEET_ALONE_TIMEOUT seconds, leave
            # with leave_reason="alone" so the caller can trigger summarization.
            leave_when_alone = os.environ.get(
                "HERMES_MEET_LEAVE_WHEN_ALONE", "1"
            ).strip().lower() not in ("0", "false", "no", "")
            alone_timeout = float(os.environ.get("HERMES_MEET_ALONE_TIMEOUT", "90"))
            ever_had_company = False
            alone_since: Optional[float] = None
            last_alone_check = 0.0
            last_participant_check = 0.0
            last_dom_dump = 0.0
            dump_caption_dom = os.environ.get(
                "HERMES_MEET_DUMP_CAPTION_DOM", "").lower() in ("1", "true", "yes")
            while not stop_flag["stop"]:
                now = time.time()
                if deadline and now > deadline:
                    state.set(leave_reason="duration_expired")
                    break

                # Realtime: once in-call, make sure the mic is UNMUTED — Meet can
                # join muted, and a muted track means no one hears the TTS even
                # though audio flows into the fake-mic. Idempotent: once unmuted
                # the button reads "Turn off microphone" and no longer matches.
                if rt["enabled"] and state.in_call and mic_unmute_attempts < 6:
                    mic_unmute_attempts += 1
                    try:
                        page.evaluate(
                            r"""()=>{const b=[...document.querySelectorAll('button')].find(x=>{
                              const s=(x.innerText||'')+' '+(x.getAttribute('aria-label')||'');
                              return /turn on microphone|включить микрофон/i.test(s);});
                              if(b)b.click();}"""
                        )
                    except Exception:
                        pass

                # Admission detection every ~3s until admitted.
                if not state.in_call and (now - last_admission_check) > 3.0:
                    last_admission_check = now
                    admitted = _detect_admission(page)
                    if admitted:
                        state.set(
                            in_call=True,
                            lobby_waiting=False,
                            joined_at=now,
                            admitted_at=now,
                        )
                    # Check denial BEFORE the timeout so a real host-denial isn't
                    # misattributed as a lobby timeout (it would otherwise wait
                    # the full window and report the wrong leave_reason).
                    elif _detect_denied(page):
                        _dump_admission_snapshot(page, out_dir, "host denied admission")
                        state.set(
                            error="host denied admission",
                            leave_reason="denied",
                        )
                        break
                    elif now > lobby_deadline:
                        _dump_admission_snapshot(page, out_dir, "lobby timeout")
                        state.set(
                            error=(
                                "lobby timeout — host never admitted the bot "
                                f"within {int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0}s"
                            ),
                            leave_reason="lobby_timeout",
                        )
                        break
                    else:
                        # Still waiting — refresh the lobby snapshot so an
                        # operator polling mid-wait can see the live page state
                        # (lobby copy, disabled "Ask to join", name field, etc.)
                        # without enabling HERMES_MEET_DEBUG_MODE.
                        _dump_admission_snapshot(page, out_dir, "waiting for admission")

                # Empty-meeting auto-end (throttled ~5s). _detect_alone is
                # conservative (False on any ambiguity), so we additionally
                # require the alone state to PERSIST for alone_timeout before
                # leaving — a momentary tile-count blip won't end the call.
                if leave_when_alone and state.in_call and (now - last_alone_check) > 5.0:
                    last_alone_check = now
                    alone = _detect_alone(page)
                    if not alone:
                        ever_had_company = True
                        alone_since = None
                    elif ever_had_company:
                        if alone_since is None:
                            alone_since = now
                        # Verbal goodbye AND everyone has since left → strong,
                        # unambiguous end: exit gracefully NOW as 'verbal_closure'
                        # instead of waiting the full alone grace (which guards
                        # against accidental drops). This is the conjunction the
                        # user asked for: closure said + then disconnected.
                        recent_closing = (
                            state.meeting_closing_at is not None
                            and (now - state.meeting_closing_at) <= 180.0
                        )
                        if recent_closing:
                            state.set(leave_reason="verbal_closure")
                            break
                        if (now - alone_since) >= alone_timeout:
                            state.set(leave_reason="alone")
                            break

                # Captions can only be turned on in-call, so enable them after
                # admission. Keep retrying (throttled ~3s) until captions
                # actually produce lines — the caption button can settle several
                # seconds after admission, and re-clicking is idempotent (once
                # on, the button reads "Turn off captions" and no longer
                # matches the "Turn on" regex).
                if (
                    state.in_call
                    and state.transcript_lines == 0
                    and (now - last_caption_enable) > 3.0
                ):
                    last_caption_enable = now
                    try:
                        page.evaluate(_enable_captions_js())
                        state.set(captions_enabled_attempted=True)
                    except Exception:
                        pass

                # Force the caption language (env-gated; runner sets Russian).
                # Decoupled from the transcript gate above: the caption-settings
                # entry only appears once the caption overlay has rendered, which
                # can be after the first lines arrive — so retry on its own
                # cadence after admission until it succeeds or we exhaust tries.
                # This is belt-and-suspenders: the language is ALSO sticky in the
                # persistent Chrome profile, which is the primary guarantee.
                if (
                    caption_lang
                    and state.in_call
                    and not caption_lang_done
                    and lang_attempts < 12
                    and (now - last_lang_try) > 3.0
                ):
                    last_lang_try = now
                    lang_attempts += 1
                    try:
                        if _set_caption_language(page, caption_lang):
                            caption_lang_done = True
                    except Exception:
                        pass

                try:
                    queued = page.evaluate("window.__hermesMeetDrain && window.__hermesMeetDrain()")
                    if isinstance(queued, list):
                        for entry in queued:
                            if not isinstance(entry, dict):
                                continue
                            speaker = str(entry.get("speaker", ""))
                            text = str(entry.get("text", ""))
                            state.record_caption(speaker=speaker, text=text)
                            # Barge-in: if the bot is currently generating
                            # audio AND a real human just spoke, cancel the
                            # in-flight response so we don't talk over them.
                            if rt["enabled"] and rt["session"] is not None:
                                if _looks_like_human_speaker(speaker, guest_name):
                                    try:
                                        cancelled = rt["session"].cancel_response()
                                        if cancelled:
                                            state.set(last_barge_in_at=now)
                                    except Exception:
                                        pass
                except Exception:
                    # Meet reloaded or we got booted — try to detect and
                    # exit gracefully rather than spinning.
                    if page.is_closed():
                        state.set(leave_reason="page_closed")
                        break

                # Fold the realtime session's byte/timestamp counters into
                # the status file so meet_status can surface them.
                if rt["session"] is not None:
                    state.set(
                        audio_bytes_out=getattr(rt["session"], "audio_bytes_out", 0),
                        last_audio_out_at=getattr(rt["session"], "last_audio_out_at", None),
                    )

                # Refresh participant count (~8s) for greeting + presence-based
                # end-detection. None = unknown (degrade gracefully).
                if state.in_call and (now - last_participant_check) > 8.0:
                    last_participant_check = now
                    pc = _get_participant_count(page)
                    if pc is not None and pc != state.participant_count:
                        state.set(participant_count=pc)

                # Diagnostic caption-DOM dump (env-gated) for fixing the
                # per-speaker row selector when Meet's DOM drifts.
                if dump_caption_dom and state.in_call and (now - last_dom_dump) > 5.0:
                    last_dom_dump = now
                    _dump_caption_dom(page, out_dir)

                # Close out any utterance that has paused, so the clean
                # transcript (transcript_clean.jsonl) tracks the live dialogue.
                state.tick_finalize(now)

                time.sleep(1.0)

            # Try to leave cleanly — click the hangup button if present. Anchor
            # on the locale-independent 'call_end' icon ligature first, then
            # RU/EN aria text, so this works whether the UI is English or
            # Russian (the old aria*="eave call" was English-only).
            try:
                page.evaluate(
                    r"""() => {
                      const b = [...document.querySelectorAll('button')].find((x) => {
                        const t = (x.innerText || '') + ' ' + (x.getAttribute('aria-label') || '');
                        return /call_end/i.test(x.innerText || '')
                          || /leave call|leave meeting|покинуть видеовстреч|покинуть вызов|покинуть звон|выйти из вызова|выйти из звон/i.test(t);
                      });
                      if (b) b.click();
                    }"""
                )
            except Exception:
                pass

            context.close()
            if browser is not None:
                browser.close()
            # v2: teardown PCM pump, speaker thread, and audio bridge.
            if rt.get("pcm_pump"):
                try:
                    rt["pcm_pump"].terminate()
                    rt["pcm_pump"].wait(timeout=3)
                except Exception:
                    pass
            if rt["speaker_stop"]:
                try:
                    rt["speaker_stop"]()
                except Exception:
                    pass
            if rt["speaker_thread"] is not None:
                try:
                    rt["speaker_thread"].join(timeout=5.0)
                except Exception:
                    pass
            if rt["session"]:
                try:
                    rt["session"].close()
                except Exception:
                    pass
            if rt["bridge"]:
                try:
                    rt["bridge"].teardown()
                except Exception:
                    pass
            # Flush any still-open utterance so transcript_clean.jsonl is
            # complete before summarization / final reads.
            state.finalize_all()
            # End-of-meeting summary signal. Only for graceful ends where the
            # bot actually attended (alone / duration / host-left / explicit
            # leave / page closed) — never for denied or lobby_timeout, which
            # request_summary() also guards by joined_at/transcript_lines.
            GRACEFUL_END = {"alone", "duration_expired", "meet_leave",
                            "page_closed", "verbal_closure", None}
            if state.leave_reason in GRACEFUL_END:
                try:
                    marker = state.request_summary()
                except Exception:
                    marker = None
                # Optional auto-trigger: a hook command (e.g. a wrapper that
                # invokes the Hermes agent / codex to run meet-post-call-summary)
                # gets the meeting dir as its single argument. Detached so bot
                # teardown isn't blocked on summarization.
                summary_cmd = os.environ.get("HERMES_MEET_SUMMARY_CMD", "").strip()
                if marker is not None and summary_cmd:
                    try:
                        import shlex
                        import subprocess
                        subprocess.Popen(
                            shlex.split(summary_cmd) + [str(state.out_dir)],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            start_new_session=True,
                        )
                    except Exception:
                        pass
            state.set(in_call=False, captioning=False, exited=True)
            return 0

    except Exception as e:
        state.set(error=f"unhandled: {e}", exited=True)
        return 1


def _try_guest_name(page, guest_name: str) -> bool:
    """If Meet is showing a guest-name field, type *guest_name* into it.

    Returns True if a field was found and filled. Meet renders the name input
    a beat *after* the "Got it" dialog is dismissed, so when we detect the
    guest flow we poll for the field (up to ~10s) instead of giving up on the
    first miss — that first-miss was why "Ask to join" stayed disabled and the
    join silently failed. After filling we dispatch an ``input`` event so
    Meet's React listener re-enables the join button. The authed path ("Join
    now", no name field) skips the poll entirely and pays no latency.
    """
    guest_flow = False
    try:
        # Bilingual: EN "Got it" / RU "Понятно". The old EN-only exact match
        # meant a RU dialog never set guest_flow, skipping the name-field poll.
        got_it = page.get_by_role(
            "button", name=re.compile(r"^(got it|понятно)$", re.I)
        ).first
        if got_it.count() and got_it.is_visible():
            got_it.click(timeout=2_000)
            page.wait_for_timeout(500)
            guest_flow = True
    except Exception:
        pass

    selectors = (
        'input[aria-label*="name" i]',
        'input[placeholder*="name" i]',
        'textarea[aria-label*="name" i]',
        'textarea[placeholder*="name" i]',
        '[role="textbox"][aria-label*="name" i]',
        '[contenteditable="true"][aria-label*="name" i]',
    )

    def _fill_first() -> bool:
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if locator.count() and locator.is_visible():
                    locator.fill(guest_name, timeout=2_000)
                    # Nudge Meet's listener so "Ask to join" un-disables.
                    try:
                        locator.evaluate(
                            "(el) => el.dispatchEvent("
                            "new Event('input', {bubbles: true}))"
                        )
                    except Exception:
                        pass
                    return True
            except Exception:
                continue
        return False

    if _fill_first():
        return True

    if guest_flow:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                page.wait_for_timeout(500)
            except Exception:
                break
            if _fill_first():
                return True

    try:
        textbox = page.get_by_role("textbox").first
        if textbox.count() and textbox.is_visible():
            textbox.fill(guest_name, timeout=2_000)
            return True
    except Exception:
        pass
    return False


def _clear_stale_singleton_lock(user_data_dir: str) -> None:
    """Remove a stale Chrome ``SingletonLock`` so a crashed previous bot can't
    force this launch into a throwaway (not-signed-in) profile.

    Chrome guards a profile dir with ``SingletonLock`` — on Linux a symlink whose
    target is ``<hostname>-<pid>``. If a prior bot died without tearing Chrome
    down, the lock lingers; ``launch_persistent_context`` then can't take the
    real profile and opens a guest one, so Meet sees an *uninvited* participant
    and hard-bounces it ("You can't join this video call"). We clear the lock
    ONLY when its PID is dead — never when a live Chrome legitimately holds it.
    """
    try:
        prof = Path(user_data_dir)
        lock = prof / "SingletonLock"
        if not lock.is_symlink():
            return  # Linux Chrome always uses a symlink; leave anything else.
        alive = False
        try:
            pid = int(os.readlink(lock).rsplit("-", 1)[-1])
            from gateway.status import _pid_exists
            alive = _pid_exists(pid)
        except Exception:
            alive = False  # unparseable / dangling → treat as stale
        if not alive:
            for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                try:
                    (prof / name).unlink()
                except (FileNotFoundError, OSError):
                    pass
    except Exception:
        pass


def _dump_admission_snapshot(page, out_dir, note: str) -> None:
    """Always-on, file-based admission diagnostics (no stdout pollution).

    When the bot can't get past the lobby, the single most useful artifact is
    what Meet was actually showing — lobby copy, disabled "Ask to join", a
    host-denial screen, an unfilled name field, etc. We capture a compact
    snapshot to ``<out_dir>/admission_debug.json`` (overwritten each call, so
    the file always reflects the latest state). This is separate from the
    verbose, opt-in ``HERMES_MEET_DEBUG_MODE`` stdout dump: a file write in a
    detached bot pollutes nothing, so it runs unconditionally and gives the
    operator (or the agent) something concrete to read after a failed join.
    """
    try:
        import json as _json
        import time as _time
        snap = page.evaluate(
            r"""
            () => ({
              url: location.href,
              text: (document.body && document.body.innerText || '').slice(0, 2000),
              buttons: Array.from(document.querySelectorAll('button')).map((b, i) => ({
                i,
                inner: (b.innerText || '').trim(),
                aria: (b.getAttribute('aria-label') || '').trim(),
                visible: !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length),
                disabled: b.disabled || b.getAttribute('aria-disabled') === 'true',
              })).filter((x) => x.visible),
            })
            """
        )
        snap["note"] = note
        snap["at"] = _time.time()
        (out_dir / "admission_debug.json").write_text(
            _json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        # Diagnostics must never break the join flow.
        pass


def _detect_admission(page) -> bool:
    """True if we're clearly past the lobby and in the call itself.

    Uses a JS-side probe because Meet's DOM structure varies by client
    version. We check several high-signal indicators and declare admission
    on the first hit:

      1. Leave-call button is present (``aria-label`` contains "eave call").
      2. Caption region has appeared (we installed the observer and it attached).
      3. The participant list container is visible.

    Conservative by default — returns False on any error.
    """
    probe = r"""
    (() => {
      const buttons = Array.from(document.querySelectorAll('button'));
      // 0) LOBBY GUARD (must run first): the lobby's hangup button has the SAME
      // aria "Leave call" + 'call_end' ligature as the in-call one, so the leave
      // button cannot distinguish lobby from call. The lobby is identified by
      // its "waiting for host" copy instead — if present, we are NOT admitted.
      const body = document.body ? (document.body.innerText || '') : '';
      // DOM-not-loaded guard: an empty/short body means the page is mid-load —
      // declaring admission here was the empty-body race that latched in_call in
      // the lobby. Wait for a real render.
      if (!body || body.trim().length < 40) return false;
      if (/please wait until|asking to be let in|wait(ing)? for the host|you'?ll join the call when|подождите,? пока|вас впуст|ожидайте|организатор.*впуст|запрос на присоединение отправлен/i.test(body)) {
        return false;
      }
      // PRE-JOIN GUARD: if a join / ask-to-join button is on screen we're on the
      // device-preview/lobby page, NOT admitted. The caption container + observer
      // can already exist here, which falsely tripped the old caption-region
      // check and latched in_call=True while still in the lobby.
      const joinBtn = buttons.find((b) => {
        const t = `${b.innerText || ''} ${b.getAttribute('aria-label') || ''}`.toLowerCase();
        return /join now|ask to join|switch here|присоедин|попросить присоедин/.test(t);
      });
      if (joinBtn) return false;
      // 1) In-call leave button by RU/EN aria text.
      const leave = buttons.find((b) => {
        const text = `${b.innerText || ''} ${b.getAttribute('aria-label') || ''}`;
        return /leave call|leave meeting|покинуть видеовстреч|покинуть вызов|покинуть звон|выйти из вызова|выйти из звон/i.test(text);
      });
      if (leave) return true;
      // 2) In-call toolbar ligatures: the hangup (call_end) + chat buttons. The
      // lobby/pre-join screens were already excluded by the guards above, and the
      // device-preview has Join/Ask-to-join (no call_end, no chat) — so on a
      // loaded non-lobby page these mean we ARE in the call. (Replaces the old
      // caption-region check, which existed pre-admission and false-positived.)
      const inCallBtn = buttons.some((b) => {
        const inner = (b.innerText || '').trim().toLowerCase();
        const aria = (b.getAttribute('aria-label') || '').toLowerCase();
        return inner === 'call_end' || inner === 'chat'
            || /chat with everyone|чат с участ/.test(aria);
      });
      if (inCallBtn) return true;
      // 3) Participants container — RU/EN aria or people/group ligature button.
      const parts = document.querySelector(
        '[aria-label*="articipants" i], [aria-label*="участник" i]'
      );
      if (parts) return true;
      const partBtn = buttons.some((b) => /^(people|group)$/i.test((b.innerText || '').trim()));
      if (partBtn) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _detect_alone(page) -> bool:
    """True when the bot is the ONLY participant left (everyone else has gone).

    Used to auto-end the meeting (and trigger summarization) once the humans
    leave. Two signals: Meet's "no one else" copy (RU+EN), and a participant
    count of 1 parsed from the people button / call header. Conservative:
    returns False on any error or ambiguity (better to linger than leave early).
    """
    probe = r"""
    (() => {
      const body = document.body ? (document.body.innerText || '') : '';
      // 1) Explicit "you're alone" copy.
      if (/no one else is here|you'?re the only one|everyone else (has )?left|waiting for others to join/i.test(body)) return true;
      if (/кроме вас,? здесь больше никого|вы единственный участник|все остальные вышли|больше никого нет/i.test(body)) return true;
      // 2) Participant count == 1 (people button / aria like "Participants, 1"
      //    or RU "Участники, 1"). Only trust an explicit numeric count.
      const els = [...document.querySelectorAll('button,[role="button"],[aria-label]')];
      for (const e of els) {
        const a = e.getAttribute('aria-label') || '';
        const m = a.match(/(participants|people|участник[аи]?)\D{0,4}(\d+)/i)
               || a.match(/(\d+)\D{0,4}(participants|people|участник)/i);
        if (m) {
          const n = parseInt(m[2] && /\d/.test(m[2]) ? m[2] : m[1], 10);
          if (n === 1) return true;
          if (n >= 2) return false;
        }
      }
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _get_participant_count(page) -> Optional[int]:
    """Best-effort participant count from Meet's people button / call-header
    aria-labels (RU+EN). Returns None when no explicit numeric count is visible
    (degrade gracefully — caller treats None as "unknown"). Drives greeting
    phrasing (1 vs team) and the presence half of verbal-closure end-detection."""
    probe = r"""
    (() => {
      const els = [...document.querySelectorAll('button,[role="button"],[aria-label]')];
      for (const e of els) {
        const a = e.getAttribute('aria-label') || '';
        const m = a.match(/(participants|people|участник[аи]?)\D{0,4}(\d+)/i)
               || a.match(/(\d+)\D{0,4}(participants|people|участник)/i);
        if (m) {
          const n = parseInt(/\d/.test(m[2] || '') ? m[2] : m[1], 10);
          if (Number.isFinite(n) && n > 0) return n;
        }
      }
      return null;
    })();
    """
    try:
        v = page.evaluate(probe)
        return int(v) if isinstance(v, (int, float)) and v > 0 else None
    except Exception:
        return None


def _dump_caption_dom(page, out_dir) -> None:
    """Diagnostic (env-gated by HERMES_MEET_DUMP_CAPTION_DOM): write the live
    caption container's structure to caption_dom.json so we can engineer the
    per-speaker row selector when Meet's DOM drifts. Captures the region's
    outerHTML plus, for several candidate selectors, the match count and sample
    innerText of each node — enough to see how speakers are nested. Overwrites
    each call (latest snapshot). Never raises (diagnostics must not break join)."""
    probe = r"""
    (() => {
      const out = { candidates: [], region: null };
      const sels = [
        '[role="region"][aria-label*="aption" i]',
        'div[jsname="dsyhDe"]',
        '[data-self-name]',
        '[class*="caption" i]',
        '.bYevke', '.nMcdL', '.KcIKyf', '.zs7s8d',  // historical row/speaker classes
      ];
      for (const sel of sels) {
        let nodes = [];
        try { nodes = [...document.querySelectorAll(sel)]; } catch (e) { continue; }
        out.candidates.push({
          sel,
          count: nodes.length,
          samples: nodes.slice(0, 6).map((n) => ({
            tag: n.tagName,
            jsname: n.getAttribute('jsname') || '',
            cls: (n.className || '').toString().slice(0, 80),
            text: (n.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 160),
            childDivs: n.querySelectorAll('div').length,
          })),
        });
      }
      const region = document.querySelector('[role="region"][aria-label*="aption" i]')
                  || document.querySelector('div[jsname="dsyhDe"]');
      if (region) out.region = (region.outerHTML || '').slice(0, 24000);
      return out;
    })();
    """
    try:
        import json as _json
        snap = page.evaluate(probe)
        (out_dir / "caption_dom.json").write_text(
            _json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _detect_denied(page) -> bool:
    """True when Meet is showing a 'you were denied' / 'no one admitted' page.

    Matches both EN and RU wordings (loose stems to survive declension). Without
    the RU variants a denied/removed RU-locale bot was misclassified as a lobby
    timeout — wrong leave_reason and a full HERMES_MEET_LOBBY_TIMEOUT wait.
    """
    probe = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      // Host denied the join request. The live wording is "Someone in the call
      // denied your request to join" (captured), plus older variants.
      if (/denied your request to join/i.test(text)) return true;
      if (/отклонил[аи]? ваш запрос|отклонил[аи]? запрос на присоединение|ваш запрос (на присоединение )?отклон/i.test(text)) return true;
      // Denied / can't join.
      if (/You can't join this video call/i.test(text)) return true;
      if (/не можете присоединиться к (этой |этому )?(видео)?(встрече|звонку|конференции)/i.test(text)) return true;
      if (/вам отказано в доступе|в доступе отказано/i.test(text)) return true;
      // Removed from the meeting.
      if (/You were removed from the meeting/i.test(text)) return true;
      if (/вас удалил[аи]? (из|со) (встречи|видеовстречи|звонка|конференции)/i.test(text)) return true;
      // No one responded to the join request.
      if (/No one responded to your request to join/i.test(text)) return true;
      if (/никто не ответил на ваш запрос/i.test(text)) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _auth_gate(page):
    """Verify the browser is signed in to Google before joining a meeting.

    Navigates to the account page and decides by the *landing URL* (robust)
    plus Google's "this browser may not be secure" automation-block text.
    URL is used instead of body-text scraping because a genuinely signed-in
    ``myaccount`` page contains settings labels like "Sign-in & security" that
    would false-positive a naive "sign in" text match. When signed out, Google
    redirects ``myaccount.google.com`` to an ``accounts.google.com`` sign-in
    flow. Returns ``(authed: bool, reason: str)``; conservative on any error.
    """
    try:
        page.goto(
            "https://myaccount.google.com/",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
    except Exception as e:
        return (False, f"account page navigation failed: {e}")
    try:
        url_now = (page.url or "").lower()
    except Exception:
        url_now = ""
    try:
        text = (
            page.evaluate("() => (document.body ? document.body.innerText || '' : '')")
            or ""
        ).lower()
    except Exception:
        text = ""
    if "may not be secure" in text:
        return (False, "google blocked this browser as insecure (automation detected)")
    if "accounts.google.com" in url_now and (
        "signin" in url_now or "servicelogin" in url_now
    ):
        return (False, f"redirected to sign-in ({url_now})")
    # Signed-out users hitting myaccount are bounced to the public marketing
    # page google.com/account/about — a reliable "not signed in" signal even
    # when stale cookies remain in the jar (Google invalidates server-side).
    if "/account/about" in url_now:
        return (False, "not signed in (bounced to account/about page)")
    if "myaccount.google.com" in url_now:
        return (True, "signed in")
    return (False, f"could not confirm signed-in state (url={url_now or 'unknown'})")


def _looks_like_human_speaker(speaker: str, bot_guest_name: str) -> bool:
    """Whether a caption line's speaker is probably a human, not our bot echo.

    Meet attributes captions to the speaker's display name. When Chrome is
    reading our fake mic, Meet still attributes captions to *our* bot name
    (because the bot is the one "speaking"). We don't want those to trigger
    barge-in. Anything else — real participant names — does.

    Conservative: unknown / blank speakers (common when caption scraping
    falls back to raw text) do NOT trigger barge-in, because we can't tell
    whether it was a human or us.
    """
    if not speaker or not speaker.strip():
        return False
    spk = speaker.strip().lower()
    if spk in {"unknown", "you", bot_guest_name.strip().lower()}:
        return False
    return True


def _click_join(page, state: _BotState) -> bool:
    """Click the Meet join/request button if it is visible.

    Meet localizes the pre-join UI, and in headless mode Playwright's
    accessible role locator can miss a visible DOM button such as Russian
    ``Присоединиться``. Try role-based labels first, then fall back to a
    direct DOM button scan by innerText/aria-label. The button can appear a
    few seconds after DOMContentLoaded, so this helper waits briefly while
    keeping the original run_bot call-site unchanged.

    Flags ``lobby_waiting`` when we hit the "waiting for host to admit you"
    state so the agent can surface that in status.
    """
    # Pre-join buttons, bilingual (RU+EN). Order matters: match the
    # ask-to-join (host-admission) variant BEFORE the plain join, because in RU
    # the affirmative button's visible text is also "Присоединиться" and only
    # its aria-label ("Отправить запрос…") marks the lobby flow.
    candidates = (
        (re.compile(r"ask to join|отправить запрос|запросить подключение", re.I), True),
        (re.compile(r"\bjoin now\b|^\s*присоединиться\s*$", re.I), False),
        (re.compile(r"join here|присоединиться здесь", re.I), False),
    )
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            page.evaluate(
                r"""
                (realtime) => {
                  const click = (rx, avoid) => {
                    const b = Array.from(document.querySelectorAll('button')).find((btn) => {
                      const text = `${btn.innerText || ''} ${btn.getAttribute('aria-label') || ''}`;
                      return rx.test(text) && !(avoid && avoid.test(text));
                    });
                    if (b) { b.click(); return true; }
                    return false;
                  };
                  // Meet shows a "Do you want people to hear you?" modal before
                  // join. In TRANSCRIBE mode the bot is receive-only → dismiss it
                  // via "Continue without microphone" (guarding the affirmative).
                  // In REALTIME mode the bot SPEAKS → click the affirmative
                  // "Use microphone" so it joins WITH a mic (else it is silent).
                  if (realtime) {
                    click(/use microphone|использовать микрофон/i);
                  } else {
                    click(/don't use microphone|do not use microphone|continue without microphone|продолжить без микрофона|не включать микрофон|без микрофона/i,
                          /use microphone|использовать микрофон/i);
                  }
                  // Dismiss onboarding tooltips ("Got it" / "Понятно").
                  click(/^\s*got it\s*$|^\s*понятно\s*$/i);
                  return true;
                }
                """,
                mode == "realtime",
            )
        except Exception:
            pass

        for name_rx, waits_for_lobby in candidates:
            try:
                btn = page.get_by_role("button", name=name_rx).first
                if btn.count() and btn.is_visible():
                    btn.click(timeout=3_000)
                    if waits_for_lobby:
                        state.set(lobby_waiting=True)
                    return True
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                r"""
                () => {
                  const labels = [
                    { rx: /ask to join|отправить запрос|запросить подключение/i, avoid: /companion|режиме companion/i, waitsForLobby: true },
                    { rx: /\bjoin now\b|^присоединиться$/i, avoid: null, waitsForLobby: false },
                  ];
                  for (const b of Array.from(document.querySelectorAll('button'))) {
                    const inner = (b.innerText || '').trim();
                    const aria = (b.getAttribute('aria-label') || '').trim();
                    const text = inner + ' ' + aria;
                    const visible = !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length);
                    const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
                    if (!visible || disabled) continue;
                    // "Join here" recovery (account already in call elsewhere):
                    // anchor on the locale-independent 'add_to_queue' icon
                    // ligature, and NEVER click "Switch here" (moves someone's call).
                    if (/(^|[^a-z])add_to_queue/i.test(inner) && !/switch here|переключиться|сменить устройство/i.test(text)) {
                      b.click();
                      return { clicked: true, inner, aria, waitsForLobby: false };
                    }
                    const match = labels.find((l) => (l.rx.test(inner) || l.rx.test(aria)) && !(l.avoid && l.avoid.test(text)));
                    if (match) {
                      b.click();
                      return { clicked: true, inner, aria, waitsForLobby: match.waitsForLobby };
                    }
                  }
                  return { clicked: false };
                }
                """
            )
            if isinstance(clicked, dict) and clicked.get("clicked"):
                if clicked.get("waitsForLobby"):
                    state.set(lobby_waiting=True)
                return True
        except Exception:
            pass
        try:
            page.wait_for_timeout(1000)
        except Exception:
            time.sleep(1.0)
    return False


def _parse_duration(raw: str) -> Optional[float]:
    """Parse ``30m`` / ``2h`` / ``90`` (seconds) → float seconds, or None."""
    if not raw:
        return None
    raw = raw.strip().lower()
    try:
        if raw.endswith("h"):
            return float(raw[:-1]) * 3600
        if raw.endswith("m"):
            return float(raw[:-1]) * 60
        if raw.endswith("s"):
            return float(raw[:-1])
        return float(raw)
    except ValueError:
        return None


if __name__ == "__main__":  # pragma: no cover — subprocess entry point
    sys.exit(run_bot())
