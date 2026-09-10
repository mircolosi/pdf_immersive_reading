"""TTSEngine ABC — all engines implement this, controller never branches on
engine type, only on whether word_timings came back non-None (§7)."""
from abc import ABC, abstractmethod

from models import AudioResult


class TTSEngine(ABC):
    @abstractmethod
    def synthesize(self, text: str, out_path: str) -> AudioResult:
        """Blocking call: synthesize text, write audio to out_path, return
        AudioResult with duration_ms and word_timings (None if unsupported)."""
