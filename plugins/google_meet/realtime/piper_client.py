"""Local Piper TTS speaker — a drop-in replacement for ``RealtimeSession``.

Why this exists: the realtime path only ever used OpenAI Realtime as a *TTS*
(text in → PCM out); the bot's "ears" are Meet captions, not the model. So we
can swap the TTS backend for a self-hosted, open-weights engine with zero
per-token cost. Piper (VITS/ONNX, MIT) outputs raw 16-bit PCM directly — no MP3
transcode (unlike edge-tts) and no network RTT (unlike OpenAI/edge) — so on a
LAN/local server it matches or beats the original's first-audio latency.

This class mirrors the surface ``RealtimeSpeaker``/``meet_bot`` rely on:
``connect()``, ``speak(text)``, ``cancel_response()``, ``close()``, plus the
``sample_rate`` / ``audio_bytes_out`` / ``last_audio_out_at`` attributes. It
streams PCM sentence-by-sentence into ``audio_sink_path`` (``speaker.pcm``),
exactly like ``RealtimeSession.speak`` appends audio deltas, so the downstream
pump → null-sink → Chrome fake-mic chain is unchanged.
"""

from __future__ import annotations

import os
import re
import threading
import time
import wave
from pathlib import Path
from typing import Optional

# Module-level voice cache: model load (~hundreds of ms) is paid once per
# process, not per utterance.
_VOICE_CACHE: dict = {}

# Default Russian voice — meetings are in Russian. Piper ships native RU voices.
DEFAULT_PIPER_VOICE = "ru_RU-denis-medium"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _split_sentences(text: str) -> list:
    """Split text into sentences so the first audio lands after the first
    sentence rather than after synthesizing the whole utterance."""
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    return parts or [text]


def _voices_dir() -> Path:
    base = os.environ.get("HERMES_MEET_PIPER_VOICES_DIR", "").strip()
    if base:
        return Path(base).expanduser()
    return Path.home() / ".hermes" / "cache" / "piper-voices"


def _resolve_voice_path(voice: str, download_dir: Path) -> str:
    """Resolve *voice* to an .onnx model path. Accepts an explicit path, an
    already-downloaded voice in *download_dir*, or downloads it via Piper's
    own ``piper.download_voices`` helper on first use."""
    import subprocess
    import sys

    # Case 1: explicit path to a model file.
    p = Path(voice).expanduser()
    if p.suffix == ".onnx" and p.is_file():
        return str(p)
    # Case 2: already downloaded by name.
    candidate = download_dir / f"{voice}.onnx"
    if candidate.is_file():
        return str(candidate)
    # Case 3: download it.
    download_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, "-m", "piper.download_voices", voice,
         "--download-dir", str(download_dir)],
        check=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
    )
    if candidate.is_file():
        return str(candidate)
    raise RuntimeError(
        f"Piper voice {voice!r} not found after download into {download_dir} "
        "(check the voice name at github.com/OHF-Voice/piper1-gpl)"
    )


class PiperSpeaker:
    """TTS-only speaker backed by local Piper. Drop-in for RealtimeSession."""

    def __init__(
        self,
        *,
        audio_sink_path,
        voice: Optional[str] = None,
        voices_dir: Optional[str] = None,
        length_scale: Optional[float] = None,
        speaker_id: int = 0,
        use_cuda: bool = False,
    ) -> None:
        self.audio_sink_path = Path(audio_sink_path) if audio_sink_path else None
        self.voice_name = voice or DEFAULT_PIPER_VOICE
        self.voices_dir = Path(voices_dir).expanduser() if voices_dir else _voices_dir()
        self.length_scale = length_scale
        self.speaker_id = speaker_id
        self.use_cuda = use_cuda
        # Updated from the loaded voice in connect(); RU medium voices are 22050.
        self.sample_rate = 22050
        self.audio_bytes_out = 0
        self.last_audio_out_at: Optional[float] = None
        self._voice = None
        self._syn_config = None
        self._cancel = threading.Event()
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        from piper import PiperVoice  # raises ImportError if piper-tts absent

        model_path = _resolve_voice_path(self.voice_name, self.voices_dir)
        cache_key = f"{model_path}::cuda={self.use_cuda}"
        if cache_key not in _VOICE_CACHE:
            _VOICE_CACHE[cache_key] = PiperVoice.load(model_path, use_cuda=self.use_cuda)
        self._voice = _VOICE_CACHE[cache_key]
        # Discover the real output sample rate so the PCM pump matches it.
        sr = None
        cfg = getattr(self._voice, "config", None)
        if cfg is not None:
            sr = getattr(cfg, "sample_rate", None)
        if isinstance(sr, int) and sr > 0:
            self.sample_rate = sr
        # Optional speed knob.
        if self.length_scale is not None:
            try:
                from piper import SynthesisConfig
                self._syn_config = SynthesisConfig(length_scale=float(self.length_scale))
            except Exception:
                self._syn_config = None

    def _synth_chunks(self, sentence: str):
        """Yield raw s16le PCM bytes for *sentence*, streaming if supported."""
        voice = self._voice
        # Preferred: streaming chunk API (piper1-gpl / OHF-Voice).
        synth = getattr(voice, "synthesize", None)
        if callable(synth):
            try:
                it = (synth(sentence, syn_config=self._syn_config)
                      if self._syn_config is not None else synth(sentence))
                for chunk in it:
                    pcm = getattr(chunk, "audio_int16_bytes", None)
                    if pcm is None and hasattr(chunk, "audio_int16_array"):
                        pcm = chunk.audio_int16_array.tobytes()
                    if pcm:
                        yield pcm
                return
            except TypeError:
                # Older signature without syn_config kw.
                for chunk in synth(sentence):
                    pcm = getattr(chunk, "audio_int16_bytes", None)
                    if pcm:
                        yield pcm
                return
        # Fallback: whole-utterance WAV → read PCM frames (non-streaming).
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            if self._syn_config is not None:
                voice.synthesize_wav(sentence, wf, syn_config=self._syn_config)
            else:
                voice.synthesize_wav(sentence, wf)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            self.sample_rate = wf.getframerate() or self.sample_rate
            yield wf.readframes(wf.getnframes())

    def speak(self, text: str, timeout: float = 30.0) -> dict:
        """Synthesize *text* and append PCM to the sink, sentence by sentence.

        Honors cancel_response() (barge-in) between chunks. Returns a small
        result dict like RealtimeSession.speak.
        """
        if self._voice is None:
            return {"ok": False, "error": "piper voice not connected"}
        self._cancel.clear()
        total = 0
        sink = None
        if self.audio_sink_path is not None:
            self.audio_sink_path.parent.mkdir(parents=True, exist_ok=True)
            sink = open(self.audio_sink_path, "ab")
        try:
            for sentence in _split_sentences(text):
                if self._cancel.is_set():
                    break
                for pcm in self._synth_chunks(sentence):
                    if self._cancel.is_set():
                        break
                    if sink is not None and pcm:
                        with self._send_lock:
                            sink.write(pcm)
                            sink.flush()
                    total += len(pcm)
                    self.audio_bytes_out += len(pcm)
                    self.last_audio_out_at = time.time()
        finally:
            if sink is not None:
                sink.close()
        return {"ok": True, "bytes": total, "cancelled": self._cancel.is_set()}

    def cancel_response(self) -> bool:
        """Barge-in: stop appending audio for the current utterance."""
        self._cancel.set()
        return True

    def close(self) -> None:
        # Keep the cached voice for reuse; just drop our reference.
        self._voice = None
