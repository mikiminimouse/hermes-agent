# Google Meet plugin — near-term TODO (reference state)

Status snapshot (2026-06-21): realtime voice works E2E (speech→Meet captions→
brain→Silero TTS→virtual mic). Default voice **eugene** (Silero). Transcribe
gates A–F pass. ~20 commits live on branch `meet/locale-receive-only` (NOT yet
durable in prod — see P0).

## P0 — Durable deployment (blocks everything)
- [ ] Our delta lives only on branch `meet/locale-receive-only`. Prod
  `~/.hermes/hermes-agent` main is pristine upstream because `hermes update`
  runs `git reset --hard origin/main` (main.py:8866) and WIPES local commits
  (confirmed via reflog).
- [ ] Fix: fork `NousResearch/hermes-agent` → set fork as `origin` (carry our
  delta on fork main), `upstream`=NousResearch. The updater already compares
  origin vs upstream — built for this layout. Then `reset --hard origin/main`
  restores OUR code.

## P1 — Auto meeting-end → auto-summary
- [ ] Detect "meeting ended / alone in call" (participant count == 1 for N s →
  leave). Currently bot only ends on duration / removed-by-host / page_closed /
  lobby_timeout / manual stop. No empty-meeting detection.
- [ ] End-of-meeting hook → auto-run skill `meet-post-call-summary` → write
  report.md. (`on_session_end` is agent-session cleanup, NOT a summary trigger.)

## P1 — In-bot LLM brain (low-latency conversation)
- [ ] Build the conversation loop INTO the bot (captions→LLM→TTS) to remove the
  main-agent-in-the-loop relay. Open question: fast-LLM access — codex
  auth.json OPENAI_API_KEY is empty; Anthropic tunnel is Claude-Code OAuth (not
  script-usable). Decide: provide an API key, or run a local small LLM.
- Current working stand-in: main agent drives via /tmp/meetctl.py (say/dump) +
  /tmp/next_utterance.py (user-utterance detector, pause 1.0s).

## P2 — Deep post-meeting summary
- [ ] Wire the deep pass: stronger model + full transcript → decisions, tasks,
  owners, deadlines, "who promised what", valuable applicable info (RU). Skill
  `meet-post-call-summary` exists; collapse rolling-caption partials first.

## P2 — Coverage tests (need a 2nd participant / settings)
- [ ] Multi-speaker name attribution (2+ humans speaking).
- [ ] Guest mode on a guest-enabled meeting (anon join was blocked by policy).

## P3 — Polish
- [ ] Add realtime deps to `install --realtime` / lazy_deps: piper-tts,
  torch (cpu), omegaconf, silero model (now manually installed in venv).
- [ ] Silero smoothness: optional whole-utterance pre-synth / larger buffer
  (Silero RTF 0.77 vs Piper 0.064; paplay --latency-msec=250 already added).
- [ ] Page-reload / disconnect recovery test.

## Decided / done
- Default voice: ru eugene (Silero); Piper ruslan = lowest-latency MIT fallback.
- Mic fixes: module-remap-source, set-default-source, tail-pump, Use-microphone
  + unmute in realtime, getUserMedia AEC/NS/AGC off, paplay 250ms buffer.
- Detection pause: 1.0s.
