"""edge-tts backend: cloud (Microsoft), async streaming, native word-boundary
timestamps. Not offline despite running via a local library — see README."""
import asyncio

import edge_tts
import soundfile as sf

from engines.base import TTSEngine
from models import AudioResult, WordTiming

# edge-tts offsets/durations are in 100-nanosecond ticks
_TICKS_PER_MS = 10_000


class EdgeTTSEngine(TTSEngine):
    def __init__(self, voice: str, rate: str = "+0%", pitch: str = "+0Hz"):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch

    def synthesize(self, text: str, out_path: str) -> AudioResult:
        return asyncio.run(self._synthesize_async(text, out_path))

    async def _synthesize_async(self, text: str, out_path: str) -> AudioResult:
        communicate = edge_tts.Communicate(
            text,
            self.voice,
            rate=self.rate,
            pitch=self.pitch,
            boundary="WordBoundary",
            # A stalled call otherwise defaults to a 60s hang, which can tie
            # up a synthesis worker slot and stall the whole prefetch queue.
            receive_timeout=15,
        )
        word_timings: list[WordTiming] = []
        with open(out_path, "wb") as f:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    f.write(chunk["data"])
                elif chunk["type"] == "WordBoundary":
                    start_ms = chunk["offset"] // _TICKS_PER_MS
                    dur_ms = chunk["duration"] // _TICKS_PER_MS
                    word_timings.append(
                        WordTiming(word=chunk["text"], start_ms=start_ms, end_ms=start_ms + dur_ms)
                    )

        duration_ms = _probe_duration_ms(out_path)
        return AudioResult(
            sentence_key="",
            audio_path=out_path,
            duration_ms=duration_ms,
            word_timings=word_timings or None,
        )


def _probe_duration_ms(path: str) -> int:
    info = sf.info(path)
    return int(info.frames / info.samplerate * 1000)
