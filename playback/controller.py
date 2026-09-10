"""State machine + sentence queue + prefetch, per design doc §10.

Single-user app (per §13), so one controller instance is a module-level
singleton in app.py rather than per-session gr.State — background threads and
a synthesis queue don't serialize across Gradio sessions anyway, and the app
is local-only / single-user by requirement.
"""
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from html import escape

from engines.base import TTSEngine
from models import AudioResult, Sentence
from pdf.converters.registry import build_converter
from pdf.renderer import render_page
from playback.cache import AudioCache
from text.markdown_inline import render_inline_html, wrap_words_in_html
from text.segmenter import segment_page, split_for_synthesis
from utils.hashing import sha256_file

logger = logging.getLogger(__name__)

_CALIBRATION_TEXT = "The quick brown fox jumps over the lazy dog near the riverbank."


def _new_temp_audio(suffix: str = ".mp3") -> str:
    """A closed, exclusively-created temp path. tempfile.mktemp() is deprecated
    (it returns a name nothing holds, so anything may claim it in between);
    mkstemp creates the file itself and we only need its path."""
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


class PlaybackController:
    def __init__(self, config: dict, engine: TTSEngine, engine_name: str, voice: str, is_local_engine: bool):
        self.config = config
        self.engine = engine
        self.engine_name = engine_name
        self.voice = voice

        self.cache_root = config["cache"]["dir"]
        self.cache_enabled = config["cache"]["enabled"]
        self.render_dpi = config["pdf"]["render_dpi"]
        self.strip_headers_footers = config["pdf"]["strip_repeated_headers_footers"]
        self.buffer_ahead = config["playback"]["buffer_ahead"]
        self.split_over_chars = config["playback"]["split_long_sentences_over_chars"]

        # §10.2: edge-tts is network-bound -> concurrent workers, sized to
        # cover a full buffer refill so a seek's synthesis never queues
        # behind routine prefetch; local engines are one in-process model
        # instance -> 1 worker (concurrent inference isn't assumed safe).
        self._executor = ThreadPoolExecutor(max_workers=1 if is_local_engine else self.buffer_ahead + 1)

        self.pdf_path: str | None = None
        self.pdf_hash: str | None = None
        self.cache: AudioCache | None = None
        self.pages_text: list[str] = []
        self.sentences_by_page: dict[int, list[Sentence]] = {}
        # page -> rendered panel HTML. Valid until the sentence list changes,
        # which is the only thing the panel now depends on.
        self._panel_html_cache: dict[int, str] = {}
        self.page_count = 0

        self.current_page = 1
        self.current_index = 0  # index within current page's sentence list
        self.state = "IDLE"  # IDLE | LOADING | PLAYING | PAUSED

        self._generation = 0
        self._ready: dict[tuple[int, int], AudioResult] = {}
        # (page, index) -> generation of the task currently synthesizing it.
        # Tagging by generation instead of using a plain set is what lets a
        # seek invalidate in-flight work *without* clearing this: a stale
        # entry no longer blocks a re-submission, and a stale task's own
        # cleanup can't delete the entry belonging to its replacement.
        self._pending: dict[tuple[int, int], int] = {}
        self._word_seek_offsets: dict[tuple[int, int], int] = {}
        # only used with cache.enabled=false, where no cache entry takes
        # ownership of the synthesized file; purged when a document is loaded
        self._temp_files: list[str] = []
        # (page, index) -> (generation that failed, message). Without this a
        # synthesis exception vanished into the executor's future: the sentence
        # simply never became ready, `poll()` kept emitting an empty src, and
        # playback sat silent with nothing on screen explaining why.
        self._errors: dict[tuple[int, int], tuple[int, str]] = {}
        self._lock = threading.Lock()

        self.rtf_warning: str | None = None
        try:
            self._calibrate()
        except Exception as exc:
            # A dead network (edge-tts) or a broken local model must not stop
            # the app from starting — the PDF is still readable on screen, and
            # the failure belongs in the UI, not in a traceback at import time.
            logger.exception("TTS calibration failed")
            self.rtf_warning = f"{self.engine_name} is not responding ({type(exc).__name__}) — playback will fail."

        self.converter_name = config["pdf"]["converter"]
        self.converter = build_converter(self.converter_name, self.strip_headers_footers)
        self.converter_warning: str | None = None

    # ---- setup -----------------------------------------------------

    def _calibrate(self):
        tmp = _new_temp_audio()
        start = time.monotonic()
        try:
            result = self.engine.synthesize(_CALIBRATION_TEXT, tmp)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        elapsed_ms = (time.monotonic() - start) * 1000
        rtf = elapsed_ms / result.duration_ms if result.duration_ms else 0
        if rtf > 1.0:
            self.rtf_warning = (
                f"{self.engine_name} is running slower than real-time on this machine "
                f"(RTF={rtf:.2f}) — expect occasional pauses."
            )

    def load_pdf(self, pdf_path: str, converter_name: str | None = None):
        self.state = "LOADING"
        self.pdf_path = pdf_path
        self.pdf_hash = sha256_file(pdf_path)

        switched_converter = converter_name and converter_name != self.converter_name
        old_converter_name = self.converter_name
        if switched_converter:
            self.converter_name = converter_name
            self.converter = build_converter(converter_name, self.strip_headers_footers)
        # cache is keyed by converter too — switching converters must not
        # collide with or reuse a previous converter's synthesized audio,
        # since the underlying markdown (and thus sentence text) differs.
        self.cache = AudioCache(
            self.cache_root, self.pdf_hash, f"{self.engine_name}__{self.converter_name}", self.voice
        )

        if switched_converter:
            old_cache_dir = os.path.join(self.cache_root, self.pdf_hash, f"{self.engine_name}__{old_converter_name}")
            shutil.rmtree(old_cache_dir, ignore_errors=True)

        self.pages_text = self.converter.extract_markdown_pages(pdf_path)
        self.page_count = len(self.pages_text)
        self.converter_warning = (
            None
            if self.converter.supports_pages
            else f"{self.converter_name} does not support per-page navigation — "
            "the whole document is shown as one page; page-image navigation is disabled."
        )
        self.sentences_by_page = {}
        for i, text in enumerate(self.pages_text, start=1):
            self.sentences_by_page[i] = segment_page(text, i, self.converter.produces_inline_markdown)

        self.current_page = 1
        self.current_index = 0
        self._reset_queue()
        self._seed_queue()
        self.state = "PAUSED"

    def set_converter(self, converter_name: str):
        """Select a converter before any PDF is loaded, or to change the
        pending choice — does not reload/reprocess a PDF; if one is already
        open, reload_with_converter() (or load_pdf again) applies it."""
        if converter_name != self.converter_name:
            self.converter_name = converter_name
            self.converter = build_converter(converter_name, self.strip_headers_footers)

    def reload_with_converter(self, converter_name: str):
        """Re-run the currently open PDF through a different converter."""
        if self.pdf_path:
            self.load_pdf(self.pdf_path, converter_name=converter_name)
        else:
            self.set_converter(converter_name)

    # ---- page / cache helpers ---------------------------------------

    def page_image_path(self, page_no: int) -> str:
        out_dir = os.path.join(self.cache_root, self.pdf_hash, "pages")
        return render_page(self.pdf_path, page_no, out_dir, self.render_dpi)

    def sentences_for(self, page_no: int) -> list[Sentence]:
        return self.sentences_by_page.get(page_no, [])

    def current_sentence(self) -> Sentence | None:
        sents = self.sentences_for(self.current_page)
        if 0 <= self.current_index < len(sents):
            return sents[self.current_index]
        return None

    def next_position(self) -> tuple[int, int] | None:
        """(page, index) of the sentence right after the current one, crossing
        a page boundary if needed. Published to the frontend so it can start
        the next clip the instant the current one ends, without waiting for
        the ended -> advance-button -> Python -> Timer round trip."""
        positions = self._upcoming_positions(2)
        return positions[1] if len(positions) > 1 else None

    # ---- synthesis / prefetch ----------------------------------------

    def _bump_generation(self):
        """A seek or page turn invalidates in-flight work and any pending
        word-seek offset — but deliberately *not* `_ready`. Those entries are
        a pure memo keyed by (page, index): the audio for sentence (3, 7) is
        still the correct audio for sentence (3, 7) after a seek. Clearing it
        made every seek and page turn go briefly silent, because `poll()`
        emits `data-src=""` for a sentence with no ready audio and the player
        then has nothing to play until synthesis lands again. Only a change to
        the sentence list itself can make these wrong — see `_reset_queue`."""
        with self._lock:
            self._generation += 1
            self._word_seek_offsets.clear()
        return self._generation

    def _reset_queue(self):
        """Full reset, for when the sentence list itself changed (new PDF or
        new converter): every (page, index) now denotes different text, so
        every memoized result and recorded error is stale."""
        self._panel_html_cache.clear()
        with self._lock:
            self._ready.clear()
            self._errors.clear()
            stale_files, self._temp_files = self._temp_files, []
        for path in stale_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self._bump_generation()

    def _upcoming_positions(self, count: int) -> list[tuple[int, int]]:
        """Next `count` (page, index) positions starting at current position,
        walking forward across page boundaries."""
        positions = []
        page, idx = self.current_page, self.current_index
        while len(positions) < count and page <= self.page_count:
            sents = self.sentences_for(page)
            if idx < len(sents):
                positions.append((page, idx))
                idx += 1
            else:
                page += 1
                idx = 0
        return positions

    def _seed_queue(self):
        generation = self._generation
        for page, idx in self._upcoming_positions(self.buffer_ahead + 1):
            self._ensure_synthesizing(page, idx, generation)

    def _ensure_synthesizing(self, page: int, idx: int, generation: int):
        key = (page, idx)
        with self._lock:
            if key in self._ready or self._pending.get(key) == generation:
                return
            failed = self._errors.get(key)
            if failed and failed[0] == generation:
                # Already failed this generation — don't spin the engine every
                # 300ms poll. A seek or page turn bumps the generation and so
                # retries, which is the user's way of asking again.
                return
            self._pending[key] = generation
        self._executor.submit(self._synthesize_one, page, idx, generation)

    _ENGINE_CALL_TIMEOUT_S = 30

    def _call_engine(self, text: str, out_path: str) -> AudioResult:
        """Run one engine.synthesize() call with a hard wall-clock timeout, on
        a disposable one-off thread rather than a slot in self._executor. A
        network stall (edge-tts) or a wedged local model call must never
        permanently shrink the shared worker pool's capacity — abandoning the
        thread on timeout (shutdown(wait=False)) costs one leaked thread, not
        one lost slot for the rest of the app's lifetime."""
        one_off = ThreadPoolExecutor(max_workers=1)
        future = one_off.submit(self.engine.synthesize, text, out_path)
        try:
            return future.result(timeout=self._ENGINE_CALL_TIMEOUT_S)
        except FutureTimeoutError:
            raise RuntimeError(
                f"{self.engine_name} synthesis exceeded {self._ENGINE_CALL_TIMEOUT_S}s — treating as failed"
            )
        finally:
            one_off.shutdown(wait=False)

    def _synthesize_one(self, page: int, idx: int, generation: int):
        key = (page, idx)
        try:
            with self._lock:
                if generation != self._generation:
                    return  # a seek made this stale before its turn in the worker queue came up
            sentences = self.sentences_for(page)
            if idx >= len(sentences):
                return  # the document was re-segmented (converter switch) while this queued
            sentence = sentences[idx]

            if self.cache_enabled:
                cached = self.cache.get(sentence)
                if cached is not None:
                    self._store_result(key, cached)
                    return

            chunks = split_for_synthesis(sentence.text, self.split_over_chars)
            if len(chunks) == 1:
                result = self._call_engine(sentence.text, _new_temp_audio())
            else:
                result = self._synthesize_chunked(chunks)

            result.sentence_key = f"{self.pdf_hash}:{self.engine_name}:{self.voice}:{page}:{idx}"
            if self.cache_enabled:
                result = self.cache.put(sentence, result)
            else:
                # nothing owns this temp file once playback moves on
                self._temp_files.append(result.audio_path)
            self._store_result(key, result)
        except Exception as exc:
            logger.exception("synthesis failed for page %d, sentence %d", page, idx)
            with self._lock:
                self._errors[key] = (generation, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                if self._pending.get(key) == generation:
                    del self._pending[key]

    def _synthesize_chunked(self, chunks: list[str]) -> AudioResult:
        """§10.3: sub-sentence chunking for synthesis granularity only — the
        chunks are concatenated back into one audio clip for playback."""
        import soundfile as sf
        import numpy as np

        pieces = []
        sample_rate = None
        for chunk in chunks:
            tmp = _new_temp_audio()
            try:
                self._call_engine(chunk, tmp)
                data, sr = sf.read(tmp)
            finally:
                os.remove(tmp)  # also on failure — otherwise a stalled chunk leaks a file
            sample_rate = sample_rate or sr
            pieces.append(data)

        combined = np.concatenate(pieces)
        out_path = _new_temp_audio(".wav")
        sf.write(out_path, combined, sample_rate)
        duration_ms = int(len(combined) / sample_rate * 1000)
        return AudioResult(sentence_key="", audio_path=out_path, duration_ms=duration_ms, word_timings=None)

    def _store_result(self, key: tuple[int, int], result: AudioResult):
        """Stored unconditionally, even if a seek happened while this was in
        flight: the audio is correct for its (page, index) regardless of when
        it arrived, and discarding finished work only buys a re-synthesis."""
        with self._lock:
            self._ready[key] = result
            self._errors.pop(key, None)

    def get_ready(self, page: int, idx: int) -> AudioResult | None:
        with self._lock:
            return self._ready.get((page, idx))

    def is_pending(self, page: int, idx: int) -> bool:
        with self._lock:
            return self._pending.get((page, idx)) == self._generation

    def error_for(self, page: int, idx: int) -> str | None:
        with self._lock:
            failed = self._errors.get((page, idx))
        return failed[1] if failed else None

    # ---- transport events ----------------------------------------------

    def play(self):
        self.state = "PLAYING"
        self._seed_queue()

    def pause(self):
        self.state = "PAUSED"

    def stop(self):
        self.state = "PAUSED"
        self.current_index = 0
        self._bump_generation()
        self._seed_queue()

    def sentence_audio_ended(self):
        """Auto-advance to next sentence, crossing page boundary if needed."""
        self.next_sentence()

    def next_sentence(self):
        sents = self.sentences_for(self.current_page)
        if self.current_index + 1 < len(sents):
            self.current_index += 1
        elif self.current_page < self.page_count:
            self.current_page += 1
            self.current_index = 0
        self._seed_queue()

    def prev_sentence(self):
        if self.current_index > 0:
            self.current_index -= 1
        elif self.current_page > 1:
            self.current_page -= 1
            self.current_index = max(0, len(self.sentences_for(self.current_page)) - 1)
        self._bump_generation()
        self._seed_queue()

    def next_page(self):
        if self.current_page < self.page_count:
            self.current_page += 1
            self.current_index = 0
            self._bump_generation()
            self._seed_queue()

    def prev_page(self):
        if self.current_page > 1:
            self.current_page -= 1
            self.current_index = 0
            self._bump_generation()
            self._seed_queue()

    def seek_to_sentence(self, page: int, idx: int):
        self.current_page = page
        self.current_index = idx
        self._bump_generation()
        self._seed_queue()

    def seek_to_word(self, page: int, idx: int, offset_ms: int):
        """Jump to a sentence and start its audio partway through, at a
        clicked word's timestamp. Cross-sentence and same-sentence clicks
        both go through here — a same-sentence click just re-seeds a cache
        hit, so it resolves in one poll tick."""
        self.seek_to_sentence(page, idx)
        with self._lock:
            self._word_seek_offsets[(page, idx)] = offset_ms
        self.state = "PLAYING"

    def word_seek_offset_ms(self, page: int, idx: int) -> int:
        with self._lock:
            return self._word_seek_offsets.get((page, idx), 0)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def render_markdown(self) -> bool:
        return self.converter.produces_inline_markdown

    # ---- rendering -------------------------------------------------

    @staticmethod
    def align_timings_to_words(text: str, word_timings) -> list[list[int] | None]:
        """Map a sentence's `WordTiming`s onto visible-word indices.

        The panel's word spans are numbered by `wrap_words_in_html`, which
        counts whitespace-delimited runs of the rendered text. This walks the
        same runs and reports `[start_ms, end_ms]` per index, or None where no
        timing covers that word.

        Anchoring is by a monotonically advancing character cursor, which is
        what makes a repeated word ("the ... the") land on its own occurrence
        rather than on the first match. edge-tts also emits several boundaries
        for one visible token ("well-known" -> "well", "known"), so a word that
        collects more than one timing keeps the union of their span. A timing
        that can't be located at all (the engine normalized a number or an
        abbreviation) contributes nothing and is skipped — it can no longer
        corrupt the rendered text, because this returns indices, not markup.
        """
        word_ranges = [(m.start(), m.end()) for m in re.finditer(r"\S+", text)]
        aligned: list[list[int] | None] = [None] * len(word_ranges)
        cursor = 0
        index = 0
        for w in word_timings:
            found = text.find(w.word, cursor)
            if found == -1:
                continue
            cursor = found + len(w.word)
            while index < len(word_ranges) and word_ranges[index][1] <= found:
                index += 1
            if index >= len(word_ranges):
                break
            existing = aligned[index]
            aligned[index] = [existing[0] if existing else w.start_ms, w.end_ms]
        return aligned

    def word_timings_json(self, page: int, idx: int) -> str:
        """`[[start,end],null,...]` for one sentence, or "" if not synthesized
        (or if the engine gives no timings). Published in the control div and
        stamped onto the existing word spans by the client — this is the whole
        "one word changed" channel that used to be a full-page re-render."""
        result = self.get_ready(page, idx)
        if not result or not result.word_timings:
            return ""
        sentences = self.sentences_for(page)
        if idx >= len(sentences):
            return ""
        return json.dumps(self.align_timings_to_words(sentences[idx].text, result.word_timings))

    def marked_sentence_ids(self) -> tuple[str, str]:
        """(pending, failed) sentence ids on the current page, comma-separated.
        Attribute toggles for the client; they used to be baked into the panel
        HTML, which is what made prefetch of *one* sentence re-render *all*."""
        pending, failed = [], []
        for s in self.sentences_for(self.current_page):
            key = (s.page_no, s.index_in_page)
            if self.get_ready(*key):
                continue
            span_id = f"sent-{s.page_no}-{s.index_in_page}"
            if self.error_for(*key):
                failed.append(span_id)
            elif self.is_pending(*key):
                pending.append(span_id)
        return ",".join(pending), ",".join(failed)

    def render_text_panel_html(self) -> str:
        """Depends on the current page's sentences and nothing else — no
        playback state, no synthesis state. That is deliberate: the panel is
        kilobytes of HTML, and rebuilding it because one sentence finished
        prefetching remounted the whole subtree (destroying live highlight
        classes) roughly every time the buffer advanced. Everything that varies
        during playback now travels as data in the control div instead, so this
        is rendered once per page and cached."""
        cached = self._panel_html_cache.get(self.current_page)
        if cached is not None:
            return cached

        parts = []
        prev_block_index = None
        for s in self.sentences_for(self.current_page):
            span_id = f"sent-{s.page_no}-{s.index_in_page}"
            rendered = render_inline_html(s.display_text) if self.render_markdown else escape(s.text)
            inner = wrap_words_in_html(rendered)

            # a new block starts on its own line; sentences within the same
            # block (e.g. several sentences of one paragraph) flow together
            if prev_block_index is not None and s.block_index != prev_block_index:
                parts.append("<br/>")
            prev_block_index = s.block_index

            if s.block_type == "heading":
                tag = f"h{min(max(s.heading_level, 1), 6)}"
                parts.append(f'<{tag} id="{span_id}" class="sentence heading">{inner}</{tag}>')
            elif s.block_type == "list_item":
                parts.append(f'<span id="{span_id}" class="sentence list-item">&bull;&nbsp;{inner}</span>')
            else:
                parts.append(f'<span id="{span_id}" class="sentence">{inner}</span>')

        html = '<div class="text-panel">' + " ".join(parts) + "</div>"
        self._panel_html_cache[self.current_page] = html
        return html
