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

class _BotState:
    """Single-process mutable state, flushed to ``status.json`` on each change."""

    def __init__(self, out_dir: Path, meeting_id: str, url: str):
        self.out_dir = out_dir
        self.meeting_id = meeting_id
        self.url = url
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
        out_dir.mkdir(parents=True, exist_ok=True)
        self.transcript_path = out_dir / "transcript.txt"
        self.status_path = out_dir / "status.json"
        self._flush()

    # -------- transcript ------------------------------------------------

    def record_caption(self, speaker: str, text: str) -> None:
        """Append a caption line if we haven't seen this exact (speaker, text)."""
        speaker = (speaker or "").strip() or "Unknown"
        text = (text or "").strip()
        if not text:
            return
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
        }
        tmp = self.status_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.status_path)

    def set(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)
        self._flush()


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
    // Each caption row carries a speaker label then the spoken text. Old class
    // selectors drift across Meet rewrites, so split the row/region innerText:
    // line 1 = speaker, the rest = text. Falls back to the whole region.
    const rows = root.querySelectorAll('div[jsname="dsyhDe"]');
    const targets = (rows && rows.length) ? rows : [root];
    targets.forEach((row) => {
      const lines = (row.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean);
      if (!lines.length) return;
      if (lines.length === 1) pushEntry('', lines[0]);
      else pushEntry(lines[0], lines.slice(1).join(' '));
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

    Clicks the real toolbar button (``aria-label`` "Turn on captions" + RU
    "Включить субтитры"), falling back to the ``c`` keyboard shortcut. The
    button only exists *in-call*, so this must run after admission — in the
    lobby it is a no-op. Idempotent: once captions are on the button reads
    "Turn off captions" and no longer matches, so re-running won't toggle them
    back off. Returns true if the button was found and clicked. The synthetic
    ``c`` keystroke alone does not work in headless Chrome — hence the click.
    """
    return r"""
    (() => {
      const btn = Array.from(document.querySelectorAll('button')).find((b) => {
        const s = `${b.innerText || ''} ${b.getAttribute('aria-label') || ''}`;
        return /turn on captions|включить субтитры/i.test(s);
      });
      if (btn) { btn.click(); return true; }
      const ev = new KeyboardEvent('keydown', {
        key: 'c', code: 'KeyC', keyCode: 67, which: 67, bubbles: true,
      });
      document.body.dispatchEvent(ev);
      return false;
    })();
    """


def _set_caption_language(page, lang: str) -> bool:
    """Open caption settings and set the meeting/caption language.

    Meet's live captions transcribe ONLY the configured language — there is no
    auto-detect — so in transcribe mode the language must match the speakers or
    the caption region stays empty. Opens Settings → Captions → "Language of
    the meeting" and picks the first option whose label matches *lang*
    (case-insensitive, e.g. "Russian" / "Русский"). Best-effort; returns True
    only when an option was actually selected. Uses Playwright role locators
    (combobox/option) rather than raw DOM clicks because the dropdown is a
    custom listbox that renders options lazily.
    """
    try:
        page.evaluate(
            "()=>{const b=[...document.querySelectorAll('button')].find("
            "x=>/open caption settings|caption settings|настройки субтитров/i"
            ".test((x.innerText||'')+' '+(x.getAttribute('aria-label')||'')));"
            "if(b)b.click()}"
        )
    except Exception:
        return False
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    rx = re.compile(lang, re.I)
    opened = False
    try:
        page.get_by_role("combobox").first.click(timeout=3_000)
        opened = True
    except Exception:
        try:
            page.get_by_label(re.compile("language", re.I)).first.click(timeout=3_000)
            opened = True
        except Exception:
            opened = False
    if not opened:
        return False
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    selected = False
    try:
        opt = page.get_by_role("option", name=rx).first
        if opt.count():
            opt.click(timeout=3_000)
            selected = True
    except Exception:
        selected = False
    # Close the settings dialog so it doesn't cover the caption region.
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

    pcm_path = out_dir / SAY_PCM_FILENAME
    queue_path = out_dir / SAY_QUEUE_FILENAME
    processed_path = out_dir / "say_processed.jsonl"
    # Reset the sink file so we start clean each session.
    pcm_path.write_bytes(b"")
    # Make sure the queue exists so the speaker poller doesn't error on
    # first iteration.
    queue_path.touch()

    try:
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
        state.set(error=f"realtime connect failed: {e}")
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
        try:
            proc = _sp.Popen(
                [
                    "paplay",
                    "--raw",
                    "--rate=24000",
                    "--format=s16le",
                    "--channels=1",
                    f"--device={sink}",
                    str(pcm_path),
                ],
                stdin=_sp.DEVNULL,
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
            )
            rt["pcm_pump"] = proc
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
                        "-f", "s16le", "-ar", "24000", "-ac", "1",
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
    state = _BotState(out_dir=out_dir, meeting_id=meeting_id, url=url)

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
    if rt["enabled"]:
        if not realtime_api_key:
            state.set(error="realtime mode requested but no API key in HERMES_MEET_REALTIME_KEY/OPENAI_API_KEY — falling back to transcribe")
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
            if user_data_dir:
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
            while not stop_flag["stop"]:
                now = time.time()
                if deadline and now > deadline:
                    state.set(leave_reason="duration_expired")
                    break

                # Admission detection every ~3s until admitted.
                if not state.in_call and (now - last_admission_check) > 3.0:
                    last_admission_check = now
                    admitted = _detect_admission(page)
                    if admitted:
                        state.set(
                            in_call=True,
                            lobby_waiting=False,
                            joined_at=now,
                        )
                    elif now > lobby_deadline:
                        state.set(
                            error=(
                                "lobby timeout — host never admitted the bot "
                                f"within {int(lobby_deadline - state.join_attempted_at) if state.join_attempted_at else 0}s"
                            ),
                            leave_reason="lobby_timeout",
                        )
                        break
                    elif _detect_denied(page):
                        state.set(
                            error="host denied admission",
                            leave_reason="denied",
                        )
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
                    # Once captions are on, set the caption language (once) so
                    # Meet transcribes the spoken language — otherwise the
                    # default (often English) yields an empty transcript for
                    # other languages. Env-gated; unset => leave Meet's default.
                    if caption_lang and not caption_lang_done:
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

                time.sleep(1.0)

            # Try to leave cleanly — click "Leave call" button if present.
            try:
                page.evaluate(
                    "() => { const b = document.querySelector('button[aria-label*=\"eave call\"]');"
                    " if (b) b.click(); }"
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
        got_it = page.get_by_role("button", name="Got it", exact=True).first
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
      const leave = buttons.find((b) => {
        const text = `${b.innerText || ''} ${b.getAttribute('aria-label') || ''}`;
        return /leave call|leave meeting|покинуть видеовстречу|покинуть вызов|покинуть звонок|выйти из вызова|выйти из звонка/i.test(text);
      });
      if (leave) return true;
      if (window.__hermesMeetInstalled) {
        const caps = document.querySelector(
          '[role="region"][aria-label*="aption" i], ' +
          'div[jsname="YSxPC"], div[jsname="tgaKEf"]'
        );
        if (caps) return true;
      }
      const parts = document.querySelector('[aria-label*="articipants" i]');
      if (parts) return true;
      return false;
    })();
    """
    try:
        return bool(page.evaluate(probe))
    except Exception:
        return False


def _detect_denied(page) -> bool:
    """True when Meet is showing a 'you were denied' / 'no one admitted' page."""
    probe = r"""
    (() => {
      const text = document.body ? document.body.innerText || '' : '';
      // English only — matches what shows up when the host denies or
      // removes a guest.
      if (/You can't join this video call/i.test(text)) return true;
      if (/You were removed from the meeting/i.test(text)) return true;
      if (/No one responded to your request to join/i.test(text)) return true;
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
    candidates = (
        ("Join now", False),
        ("Ask to join", True),
        ("Присоединиться", False),
        # "Join here" appears when this Google account is already in the call
        # on another device (e.g. a crashed/ghosted prior bot session). It
        # joins immediately as a second instance — useful for recovery. We do
        # NOT click "Switch here": that would move someone else's call.
        ("Join here", False),
    )
    deadline = time.time() + 20.0
    while time.time() < deadline:
        try:
            page.evaluate(
                r"""
                () => {
                  const click = (rx) => {
                    const b = Array.from(document.querySelectorAll('button')).find((btn) => {
                      const text = `${btn.innerText || ''} ${btn.getAttribute('aria-label') || ''}`;
                      return rx.test(text);
                    });
                    if (b) { b.click(); return true; }
                    return false;
                  };
                  // Receive-only (transcribe) mode has no microphone, so Meet
                  // shows a "Do you want people to hear you?" modal that OVERLAYS
                  // the join button. Dismiss it via "Continue without microphone"
                  // (and the older "don't use microphone" wording + RU variants),
                  // but never match the affirmative "Use microphone" button.
                  click(/don't use microphone|do not use microphone|continue without microphone|не включать микрофон|продолжить без микрофона|без микрофона/i);
                  // Dismiss onboarding tooltips ("Got it" / "Понятно") that can
                  // cover the join controls (e.g. the "Switch here" coachmark).
                  click(/^\s*got it\s*$|^\s*понятно\s*$/i);
                  return true;
                }
                """
            )
        except Exception:
            pass

        for label, waits_for_lobby in candidates:
            try:
                btn = page.get_by_role("button", name=label, exact=False).first
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
                    { rx: /^Join now$/i, waitsForLobby: false },
                    { rx: /^Ask to join$/i, waitsForLobby: true },
                    { rx: /^Присоединиться$/i, waitsForLobby: false },
                    // Not anchored: the button's innerText is prefixed with the
                    // material-icon ligature, e.g. "add_to_queueJoin here".
                    { rx: /join here/i, waitsForLobby: false },
                  ];
                  for (const b of Array.from(document.querySelectorAll('button'))) {
                    const inner = (b.innerText || '').trim();
                    const aria = (b.getAttribute('aria-label') || '').trim();
                    const visible = !!(b.offsetWidth || b.offsetHeight || b.getClientRects().length);
                    const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
                    if (!visible || disabled) continue;
                    const match = labels.find((label) => label.rx.test(inner) || label.rx.test(aria));
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
