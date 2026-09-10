"""Coqui XTTS-v2 local neural TTS. Default speaker only, no voice cloning.
Model loaded once. No word timings -> sentence-level highlighting only."""
from engines.base import TTSEngine
from models import AudioResult


class XTTSEngine(TTSEngine):
    MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"

    def __init__(self, device: str = "cpu", speaker: str = "default"):
        from TTS.api import TTS  # deferred import, optional dependency

        self.tts = TTS(self.MODEL_NAME).to(device)
        self.speaker = self.tts.speakers[0] if speaker == "default" and self.tts.speakers else speaker
        self.language = "en"

    def synthesize(self, text: str, out_path: str) -> AudioResult:
        self.tts.tts_to_file(
            text=text, speaker=self.speaker, language=self.language, file_path=out_path
        )

        import soundfile as sf

        info = sf.info(out_path)
        duration_ms = int(info.frames / info.samplerate * 1000)

        return AudioResult(sentence_key="", audio_path=out_path, duration_ms=duration_ms, word_timings=None)
