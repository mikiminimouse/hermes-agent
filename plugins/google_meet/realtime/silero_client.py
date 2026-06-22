"""Local Silero TTS speaker — drop-in TTS speaker for ``RealtimeSession`.

Silero (snakers4/silero-models) is a community-favorite Russian TTS: runs on
CPU (no GPU), free, with several natural voices. Like the TTS adapter it only
needs to append raw s16le PCM to ``speaker.pcm``; the pump/null-sink/fake-mic
chain is unchanged. Selected via HERMES_MEET_TTS=silero.

NOTE on licensing: the Silero *model* weights carry their own (non-MIT) license
— fine for evaluation; verify terms before commercial use. MIT remains
the default for that reason.

Surface mirrors TTS interface: connect / speak / cancel_response / close, plus
sample_rate / audio_bytes_out / last_audio_out_at.
"""

from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

_MODEL_CACHE: dict = {}

# v4_ru speakers: aidar, baya, kseniya, xenia, eugene, random
DEFAULT_SILERO_VOICE = "eugene"
DEFAULT_SILERO_MODEL = "v4_ru"
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")


def _split_sentences(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in _SENTENCE_SPLIT.split(text) if p.strip()]
    return parts or [text]


class SileroSpeaker:
    """TTS-only speaker backed by local Silero. Drop-in for RealtimeSession."""

    def __init__(
        self,
        *,
        audio_sink_path,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        sample_rate: int = 48000,
    ) -> None:
        self.audio_sink_path = Path(audio_sink_path) if audio_sink_path else None
        self.voice_name = voice or DEFAULT_SILERO_VOICE
        self.model_name = model or DEFAULT_SILERO_MODEL
        # Silero supports 8000/24000/48000; 48k = best quality.
        self.sample_rate = sample_rate if sample_rate in (8000, 24000, 48000) else 48000
        self.audio_bytes_out = 0
        self.last_audio_out_at: Optional[float] = None
        self._model = None
        self._cancel = threading.Event()
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        import torch  # raises ImportError if torch absent

        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
        cache_key = f"silero::{self.model_name}"
        if cache_key not in _MODEL_CACHE:
            # torch.hub downloads + caches under ~/.cache/torch/hub on first use.
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language="ru",
                speaker=self.model_name,
                trust_repo=True,
            )
            model.to("cpu")
            _MODEL_CACHE[cache_key] = model
        self._model = _MODEL_CACHE[cache_key]
        # Warm up: the FIRST apply_tts() pays a cold-start cost (graph/alloc/JIT)
        # that otherwise lands on the greeting and can make it arrive late or get
        # dropped. Do one throwaway synth now (NOT written to the sink, so nothing
        # leaks into the meeting) — connect() returns only once the speaker can
        # produce audio instantly, so realtime_ready truly means "can speak".
        try:
            self._synth_pcm("Готово.")
        except Exception:
            pass

    def _synth_pcm(self, sentence: str) -> bytes:
        import numpy as np

        audio = self._model.apply_tts(
            text=sentence, speaker=self.voice_name, sample_rate=self.sample_rate
        )
        arr = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
        # float32 [-1,1] -> s16le PCM
        arr = np.clip(arr, -1.0, 1.0)
        return (arr * 32767.0).astype("<i2").tobytes()

    def speak(self, text: str, timeout: float = 30.0) -> dict:
        if self._model is None:
            return {"ok": False, "error": "silero model not connected"}
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
                try:
                    pcm = self._synth_pcm(sentence)
                except Exception:
                    continue
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
        self._cancel.set()
        return True

    def close(self) -> None:
        self._model = None
