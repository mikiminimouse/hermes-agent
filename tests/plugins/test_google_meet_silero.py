from __future__ import annotations


class _FakeRuOnlyModel:
    def __init__(self):
        self.seen: list[str] = []

    def apply_tts(self, *, text, speaker, sample_rate):  # noqa: ARG002
        self.seen.append(text)
        if any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in text):
            raise ValueError("latin text is unsupported")

        class _Audio:
            def detach(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                import numpy as np

                return np.array([0.0, 0.1, -0.1], dtype="float32")

        return _Audio()


def test_silero_speaker_phonetizes_latin_tokens_before_tts(tmp_path):
    from plugins.google_meet.realtime.silero_client import SileroSpeaker

    speaker = SileroSpeaker(audio_sink_path=tmp_path / "speaker.pcm")
    fake = _FakeRuOnlyModel()
    speaker._model = fake

    result = speaker.speak("Проверь NotebookLM и Google Meet.")

    assert result["ok"] is True
    assert result["bytes"] > 0
    assert fake.seen
    assert all(not any("A" <= ch <= "Z" or "a" <= ch <= "z" for ch in text) for text in fake.seen)
    assert (tmp_path / "speaker.pcm").stat().st_size > 0
