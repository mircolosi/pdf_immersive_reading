"""Kokoro local neural TTS. Model loaded once at construction (module-level
singleton usage happens in app.py, not here). No word timings -> sentence-
level highlighting only."""
import numpy as np
import soundfile as sf

from engines.base import TTSEngine
from models import AudioResult


class KokoroEngine(TTSEngine):
    def __init__(self, voice: str, device: str = "cpu"):
        from kokoro import KPipeline  # deferred import, optional dependency

        lang_code = voice[0] if voice else "a"
        self.pipeline = KPipeline(lang_code=lang_code, device=device)
        self.voice = voice

    def synthesize(self, text: str, out_path: str) -> AudioResult:
        audio_chunks = [audio for _, _, audio in self.pipeline(text, voice=self.voice)]
        audio = audio_chunks[0] if len(audio_chunks) == 1 else np.concatenate(audio_chunks)

        sample_rate = 24000
        sf.write(out_path, audio, sample_rate)
        duration_ms = int(len(audio) / sample_rate * 1000)

        return AudioResult(sentence_key="", audio_path=out_path, duration_ms=duration_ms, word_timings=None)
