# Google Meet plugin — near-term TODO (reference state)

Status snapshot (2026-06-21): realtime voice works E2E (speech→Meet captions→
brain→Silero TTS→virtual mic). Default voice **eugene** (Silero). Transcribe
gates A–F pass. ~20 commits live on branch `meet/locale-receive-only` (NOT yet
durable in prod — see P0).

## P0 — Durable deployment (blocks everything) — PRE-FORK FIXES ✅ DONE
- [x] **3 blocking issues identified & fixed (commit 2033ca6b4):**
  - Fix #1: Debug output gated under `HERMES_MEET_DEBUG_MODE` → no JSON pollution in prod stdout
  - Fix #2: TTS env vars (HERMES_MEET_TTS, HERMES_MEET_PIPER_*, HERMES_MEET_SILERO_*) added to process_manager passthrough → Piper/Silero work in clean containers
  - Fix #3: Cached `mode` variable used consistently (lines 847, 1516) → atomic realtime/transcribe decision
- [ ] Create fork `NousResearch/hermes-agent` → set fork as `origin` (carry our
  delta on fork main), `upstream`=NousResearch. The updater already compares
  origin vs upstream — built for this layout. Then `reset --hard origin/main`
  restores OUR code.
- [ ] Verify post-fork: `git log origin/main` shows our commits (2033ca6b4 + 23 prior)

## P1 — Auto meeting-end → auto-summary  ✅ DONE (on branch)
- [x] Empty-meeting detection: `_detect_alone()` (RU+EN copy + participant
  count==1), armed only after company was seen (`ever_had_company`), persist
  grace `HERMES_MEET_ALONE_TIMEOUT` (90s) → leave_reason="alone".
- [x] End-of-meeting signal: on graceful end (alone/duration/leave/page_closed)
  bot writes `summary_request.json` marker (only if attended w/ captions) and
  fires `HERMES_MEET_SUMMARY_CMD <meeting-dir>` detached. Bot never calls the LLM.
- [x] Summarizer `meet_summarize.py` + wrapper `run_summary.sh`
  (HERMES_MEET_SUMMARY_CMD target): collapse_transcript() rebuilds who-said-what
  from rolling captions (3.5 MB → 13 KB), then `codex exec` (codex-only, no API
  key) + meet-post-call-summary methodology → report.md; marker → done/failed.
  Verified E2E: real dialogue → full report; single-speaker tests → "no content".
- [ ] PROD wiring (with P0 back-port): set in verter gateway systemd drop-in
  `~/.config/systemd/user/hermes-gateway-verter.service.d/`:
  `Environment=HERMES_MEET_SUMMARY_CMD=<repo>/plugins/google_meet/run_summary.sh`
  (+ optional HERMES_MEET_ALONE_TIMEOUT). Stand wired in /tmp/meetctl.py.

## P1 — In-bot LLM brain (low-latency conversation)
- [ ] Build the conversation loop INTO the bot (captions→LLM→TTS) to remove the
  main-agent-in-the-loop relay. Open question: fast-LLM access — codex
  auth.json OPENAI_API_KEY is empty; Anthropic tunnel is Claude-Code OAuth (not
  script-usable). Decide: provide an API key, or run a local small LLM.
- Current working stand-in: main agent drives via /tmp/meetctl.py (say/dump) +
  /tmp/next_utterance.py (user-utterance detector, pause 1.0s).

## P2 — Deep post-meeting summary  (mostly covered by meet_summarize.py)
- [x] Full transcript → decisions, tasks, owners, deadlines, "who promised
  what", valuable applicable info (RU). Rolling-caption collapse done in code.
- [ ] Optional depth bump: codex ran with reasoning effort "none"; raise via
  `-c model_reasoning_effort=...` / pick a stronger model for long calls.

## P2 — Coverage tests (need a 2nd participant / settings)
- [ ] Multi-speaker name attribution (2+ humans speaking).
- [ ] Guest mode on a guest-enabled meeting (anon join was blocked by policy).

## P3 — Code quality / upstream audit
- [ ] Сравнить нашу ветку с upstream (см. /tmp/audit_prompt.md):
  - Какие расширения готовы для PR в NousResearch/hermes-agent?
  - Какие нужны правки стиля / параметризации?
  - Возможна ли интеграция как feature branch или нужен fork?
  
## P3 — Polish
- [ ] Add realtime deps to `install --realtime` / lazy_deps: piper-tts,
  torch (cpu), omegaconf, silero model (now manually installed in venv).
- [ ] Silero smoothness: optional whole-utterance pre-synth / larger buffer
  (Silero RTF 0.77 vs Piper 0.064; paplay --latency-msec=250 already added).
- [ ] Page-reload / disconnect recovery test.

## P4 — MCP integration (future, not blocked)
- [ ] Expose meet-bot control as MCP server (vs. current /tmp/meetctl.py + Bash).
  Allows Claude Code / subagents to join/control/listen without shell wrappers.
  Follows pattern of existing playwright-MCP, codex-MCP (same as hermes agent).
  Deferred until bot is stable in prod (P0–P3).

## Decided / done
- Default voice: ru eugene (Silero); Piper ruslan = lowest-latency MIT fallback.
- Mic fixes: module-remap-source, set-default-source, tail-pump, Use-microphone
  + unmute in realtime, getUserMedia AEC/NS/AGC off, paplay 250ms buffer.
- Detection pause: 1.0s.
