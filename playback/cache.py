"""On-disk audio+timing cache, per (pdf, engine, voice, sentence). §12."""
import json
import os
import re

from models import AudioResult, Sentence, WordTiming

_NON_SPEAKABLE = re.compile(r"[^0-9a-z]+")


def _spoken_key(text: str) -> str:
    """Letters and digits only, lowercased — the part of a string a TTS engine
    actually voices, with word-split and punctuation differences ignored."""
    return _NON_SPEAKABLE.sub("", text.lower())


class AudioCache:
    def __init__(self, cache_dir: str, pdf_hash: str, engine: str, voice: str):
        self.dir = os.path.join(cache_dir, pdf_hash, engine, voice)
        os.makedirs(self.dir, exist_ok=True)

    def _paths(self, sentence: Sentence) -> tuple[str, str]:
        base = f"sent_{sentence.key_prefix}"
        return (
            os.path.join(self.dir, base + ".mp3"),
            os.path.join(self.dir, base + ".json"),
        )

    def get(self, sentence: Sentence) -> AudioResult | None:
        audio_path, meta_path = self._paths(sentence)
        if not (os.path.exists(audio_path) and os.path.exists(meta_path)):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            # a sidecar truncated by a crash mid-write must read as a miss,
            # not take down the synthesis worker that asked for it
            return None
        raw_timings = meta.get("word_timings")
        timings = [WordTiming(**w) for w in raw_timings] if raw_timings is not None else None
        if not self._is_valid_for(sentence, meta, timings):
            return None
        return AudioResult(
            sentence_key=meta.get("sentence_key", ""),
            audio_path=audio_path,
            duration_ms=meta.get("duration_ms", 0),
            word_timings=timings,
        )

    def _is_valid_for(self, sentence: Sentence, meta: dict, timings: list[WordTiming] | None) -> bool:
        """An entry is only valid for the exact text it was synthesized from.
        Its path is keyed by page/index, which survives a re-segmentation of
        the same PDF that *changes* a sentence's text (HTML markup no longer
        spoken, a sentence-splitting fix) — replaying such an entry would
        speak the old text and re-render the panel from its stale word
        timings, putting the old markup back on screen."""
        if "text" in meta:
            return meta["text"] == sentence.text
        # Legacy entry, written before "text" was recorded. Discarding all of
        # those wholesale would re-synthesize a whole document's worth of
        # still-correct audio — audible as a long silence on every seek — so
        # validate it against its word timings instead: they are the record
        # of what was actually voiced, and they are also exactly what the
        # panel re-renders from, so agreeing with them rules out both
        # symptoms. An entry with no timings to check (Kokoro/XTTS) carries
        # no such evidence and has to be re-synthesized.
        if timings is None:
            return False
        return _spoken_key("".join(w.word for w in timings)) == _spoken_key(sentence.text)

    def put(self, sentence: Sentence, result: AudioResult) -> AudioResult:
        audio_path, meta_path = self._paths(sentence)
        if result.audio_path != audio_path:
            os.replace(result.audio_path, audio_path)
        meta = {
            "sentence_key": result.sentence_key,
            "text": sentence.text,
            "duration_ms": result.duration_ms,
            "word_timings": (
                [w.__dict__ for w in result.word_timings] if result.word_timings is not None else None
            ),
        }
        # write-then-rename: the audio file already exists at this point, so a
        # crash midway through a plain write would leave a half-written sidecar
        # next to valid audio — an entry that looks present but can't be read
        with open(meta_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(meta_path + ".tmp", meta_path)
        result.audio_path = audio_path
        return result

    def audio_path_for(self, sentence: Sentence) -> str:
        return self._paths(sentence)[0]
