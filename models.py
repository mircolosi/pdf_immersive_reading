"""Shared data model, per design doc §6."""
from dataclasses import dataclass


@dataclass
class Sentence:
    page_no: int
    index_in_page: int
    text: str  # cleaned, speakable — no markdown markup — used for TTS
    char_start: int
    char_end: int
    display_text: str = ""  # original, with inline markdown markup — for rendering
    block_type: str = "paragraph"  # "paragraph" | "heading" | "list_item"
    heading_level: int = 0  # 1-6 for headings, unused otherwise
    block_index: int = 0  # groups sentences that came from the same markdown block

    @property
    def key_prefix(self) -> str:
        return f"{self.page_no:04d}_{self.index_in_page:04d}"


@dataclass
class WordTiming:
    word: str
    start_ms: int
    end_ms: int


@dataclass
class AudioResult:
    sentence_key: str
    audio_path: str
    duration_ms: int
    word_timings: list[WordTiming] | None
