"""Smallest runnable self-check for the non-trivial logic: segmentation,
chunking, cache round-trip, and controller seek/generation invalidation.
No TTS network calls — uses a fake in-process engine. Run: python tests/test_pipeline.py
"""
import logging
import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import soundfile as sf

from engines.base import TTSEngine
from models import AudioResult
from playback.cache import AudioCache
from playback.controller import PlaybackController
from text.markdown_inline import (
    render_inline_html,
    strip_html_tags,
    strip_inline_markdown,
    wrap_words_in_html,
)
from text.segmenter import segment_page, split_for_synthesis
from utils.config import load_config


# distinctive word from controller._CALIBRATION_TEXT, so a deliberately broken
# engine can still answer the constructor's calibration call
_CALIBRATION_MARKER = "riverbank"


class FakeEngine(TTSEngine):
    def __init__(self, delay_s=0.0):
        self.delay_s = delay_s
        self.calls = 0

    def synthesize(self, text, out_path):
        self.calls += 1
        time.sleep(self.delay_s)
        sr = 16000
        dur_s = max(0.05, 0.05 * len(text.split()))
        audio = np.zeros(int(sr * dur_s), dtype="float32")
        sf.write(out_path, audio, sr)
        return AudioResult(sentence_key="", audio_path=out_path, duration_ms=int(dur_s * 1000), word_timings=None)


def make_sample_pdf(path):
    import pymupdf

    doc = pymupdf.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_textbox(
            (72, 100, 540, 700),
            f"Sentence one on page {i + 1}. Sentence two, with a comma. Sentence three wraps up.",
            fontsize=12,
        )
    doc.save(path)


def test_segmentation():
    sents = segment_page("First sentence. Second sentence! Third?", page_no=1)
    assert [s.text for s in sents] == ["First sentence.", "Second sentence!", "Third?"], sents


def test_segmentation_ignores_line_wraps():
    # a sentence wrapped across PDF lines must NOT split on the line breaks,
    # only on punctuation — this was the reported bug.
    wrapped = "This is one long sentence that\nwraps across several PDF\nlines before it ends. Second one."
    sents = segment_page(wrapped, page_no=1)
    assert [s.text for s in sents] == [
        "This is one long sentence that wraps across several PDF lines before it ends.",
        "Second one.",
    ], sents


def test_segmentation_et_al_is_not_a_sentence_break():
    # "et al." followed by a capitalized word (common in citations) must not
    # be treated as a sentence end — this was a reported bug.
    text = "This was shown by Smith et al. In their groundbreaking study, results were surprising. Second sentence."
    sents = segment_page(text, page_no=1)
    assert [s.text for s in sents] == [
        "This was shown by Smith et al. In their groundbreaking study, results were surprising.",
        "Second sentence.",
    ], sents


def test_segmentation_markdown_blocks():
    md = "# Title\n\nFirst para sentence one. First para sentence two.\n\n- item one\n- item two"
    sents = segment_page(md, page_no=1)
    assert [(s.block_type, s.text) for s in sents] == [
        ("heading", "Title"),
        ("paragraph", "First para sentence one."),
        ("paragraph", "First para sentence two."),
        ("list_item", "item one"),
        ("list_item", "item two"),
    ], sents
    assert sents[1].block_index == sents[2].block_index  # same paragraph block
    assert sents[3].block_index != sents[4].block_index  # each list item its own block


def test_strip_html_tags():
    assert strip_html_tags("water<sup>2</sup>O") == "water2O"
    assert strip_html_tags("H<sub>2</sub>O") == "H2O"
    # whitespace *inside* an inline tag pair goes with the tags
    assert strip_html_tags("See note<sup> 2 </sup>.") == "See note2."
    # ...but the whitespace around the pair stays
    assert strip_html_tags("This is <b>bold</b> text") == "This is bold text"
    # block-level tags leave a space, so their neighbours don't fuse
    assert strip_html_tags("a<br/>b") == "a b"
    assert strip_html_tags("<td>Cell one</td><td>Cell two</td>") == " Cell one Cell two "
    # attributes, including a quoted value containing ">", and namespaced names
    assert strip_html_tags('<span id="page-1-0"></span>Marker') == "Marker"
    assert strip_html_tags('<img src="x.png" alt="a > b">tail') == " tail"
    assert strip_html_tags("<mml:math><mml:mi>x</mml:mi></mml:math> equals") == " x equals"
    # comments (docling's "<!-- image -->" placeholder) are not spoken
    assert strip_html_tags("<!-- image -->\nFigure 1").strip() == "Figure 1"
    # entities become the character they stand for, not their name
    assert strip_html_tags("Smith &amp; Jones &nbsp;said &lt;hi&gt;") == "Smith & Jones said <hi>"
    # inequality signs must survive — not every "<"/">"  is a tag
    assert strip_html_tags("if x < 5 and y > 3") == "if x < 5 and y > 3"

    sents = segment_page("The result<sup> 2 </sup> was significant.", page_no=1)
    assert sents[0].text == "The result2 was significant."
    assert sents[0].display_text == "The result2 was significant."


def test_inline_markdown():
    raw = "This is **bold** and *italic* and `code` and a [link](http://x.com)."
    assert strip_inline_markdown(raw) == "This is bold and italic and code and a link."
    html = render_inline_html(raw)
    assert "<strong>bold</strong>" in html
    assert "<em>italic</em>" in html
    assert "<code>code</code>" in html
    assert '<a href="http://x.com"' in html
    assert ">link</a>" in html

    sents = segment_page("This is **bold** text.", page_no=1)
    assert sents[0].text == "This is bold text."  # spoken/TTS text is clean
    assert sents[0].display_text == "This is **bold** text."  # raw markup kept for display


def test_inline_markdown_passes_do_not_corrupt_each_other():
    # Each pass rewrites the whole string, so whatever a pass emits is still on
    # the table for the next one. That produced genuinely broken HTML.
    html = render_inline_html("A [link](http://x.com) plus var_name_two here.")
    # the "_" in the link's own target="_blank" used to pair with the "_" in
    # var_name_two and swallow the tag
    assert '<a href="http://x.com" target="_blank" rel="noopener">link</a>' in html
    assert "<em>" not in html

    # inline code is literal — its content is not emphasis
    assert render_inline_html("Code `a**b**c` is literal.") == "Code <code>a**b**c</code> is literal."
    assert strip_inline_markdown("Code `a**b**c` is literal.") == "Code a**b**c is literal."

    # CommonMark: intraword underscores are not emphasis. snake_case
    # identifiers are common in the PDFs this app reads.
    assert render_inline_html("variable_name_here") == "variable_name_here"
    assert strip_inline_markdown("variable_name_here") == "variable_name_here"
    assert render_inline_html("Real _emphasis_ works.") == "Real <em>emphasis</em> works."

    # emphasis inside link text still renders; the href is left alone
    assert (
        render_inline_html("[**bold link**](http://x.com/a_b)")
        == '<a href="http://x.com/a_b" target="_blank" rel="noopener"><strong>bold link</strong></a>'
    )


def test_plain_text_literal_markup_chars_survive_intact():
    # plain text was never markdown — a literal asterisk/underscore/bracket
    # (citation markers, variable names, footnotes) must survive byte-for-
    # byte, not get eaten as if it were markdown syntax.
    raw = "Effects were strong* and moderate** across trials, p < 0.001. See [1] for variable_name_here."
    sents = segment_page(raw, page_no=1, produces_inline_markdown=False)
    joined = " ".join(s.text for s in sents)
    assert "strong*" in joined
    assert "moderate**" in joined
    assert "[1]" in joined
    assert "variable_name_here" in joined
    assert sents[0].text == sents[0].display_text  # no speech/display split when not markdown


def test_chunking():
    assert split_for_synthesis("short", 100) == ["short"]
    chunks = split_for_synthesis("a, b, c, d, e, f, g, h, i, j", 5)
    assert len(chunks) > 1
    assert "".join(chunks).replace(" ", "") == "a,b,c,d,e,f,g,h,i,j".replace(" ", "")


def test_cache_roundtrip(tmp_dir):
    from models import Sentence, WordTiming

    cache = AudioCache(tmp_dir, "hash1", "fake", "v1")
    sentence = Sentence(page_no=1, index_in_page=0, text="hi", char_start=0, char_end=2)
    tmp_audio = tempfile.mktemp(suffix=".mp3")
    sf.write(tmp_audio, np.zeros(1600, dtype="float32"), 16000)
    result = AudioResult(
        sentence_key="k1", audio_path=tmp_audio, duration_ms=100, word_timings=[WordTiming("hi", 0, 100)]
    )
    cache.put(sentence, result)

    fetched = cache.get(sentence)
    assert fetched is not None
    assert fetched.duration_ms == 100
    assert fetched.word_timings[0].word == "hi"
    assert os.path.exists(fetched.audio_path)


def test_cache_miss_when_sentence_text_changed(tmp_dir):
    from models import Sentence, WordTiming

    cache = AudioCache(tmp_dir, "hash1", "fake", "v1")
    stale = Sentence(
        page_no=1, index_in_page=0, text="The result<sup>2</sup> was.", char_start=0, char_end=27
    )
    tmp_audio = tempfile.mktemp(suffix=".mp3")
    sf.write(tmp_audio, np.zeros(1600, dtype="float32"), 16000)
    cache.put(
        stale,
        AudioResult(
            sentence_key="k1",
            audio_path=tmp_audio,
            duration_ms=100,
            word_timings=[WordTiming("<sup>2</sup>", 0, 100)],
        ),
    )

    # same page/index — same cache path — but the segmenter now produces
    # clean text: replaying the old entry would speak the markup and
    # re-render the panel from its stale word timings
    fresh = Sentence(page_no=1, index_in_page=0, text="The result2 was.", char_start=0, char_end=16)
    assert cache.get(fresh) is None
    assert cache.get(stale) is not None


def test_legacy_cache_entry_is_kept_when_its_word_timings_still_match(tmp_dir):
    # Entries written before "text" was recorded must not all be thrown away:
    # re-synthesizing a whole document's worth of still-correct audio is
    # audible as a long silence on every seek. Their word timings say what
    # was voiced, so they can vouch for the entry themselves.
    import json

    from models import Sentence

    cache = AudioCache(tmp_dir, "hash1", "fake", "v1")

    def write_legacy_entry(sentence, spoken_words):
        audio_path, meta_path = cache._paths(sentence)
        sf.write(audio_path, np.zeros(1600, dtype="float32"), 16000)
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "sentence_key": "k1",
                    "duration_ms": 100,
                    "word_timings": [
                        {"word": w, "start_ms": i * 10, "end_ms": i * 10 + 10}
                        for i, w in enumerate(spoken_words)
                    ],
                },
                f,
            )

    unchanged = Sentence(page_no=1, index_in_page=0, text="The result2 was.", char_start=0, char_end=16)
    write_legacy_entry(unchanged, ["The", "result2", "was."])
    assert cache.get(unchanged) is not None  # replayed, no re-synthesis

    changed = Sentence(page_no=2, index_in_page=0, text="The result2 was.", char_start=0, char_end=16)
    write_legacy_entry(changed, ["The", "result", "sup", "2", "sup", "was."])  # spoke the markup
    assert cache.get(changed) is None

    no_evidence = Sentence(page_no=3, index_in_page=0, text="No timings here.", char_start=0, char_end=16)
    audio_path, meta_path = cache._paths(no_evidence)
    sf.write(audio_path, np.zeros(1600, dtype="float32"), 16000)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump({"sentence_key": "k1", "duration_ms": 100, "word_timings": None}, f)
    assert cache.get(no_evidence) is None


def test_controller_seek_bumps_generation_but_keeps_ready_audio(tmp_dir, pdf_path):
    # A seek must invalidate the *word-seek offset* and tell the frontend to
    # reload, but NOT throw away already-synthesized audio: those entries are
    # keyed by (page, index) and stay correct across a seek. Clearing them made
    # every seek and page turn go briefly silent, because poll() emits an empty
    # data-src for a sentence with no ready audio.
    config = {
        "cache": {"dir": tmp_dir, "enabled": True},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 2, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, FakeEngine(delay_s=0.05), "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)
    ctrl.play()
    for _ in range(40):
        if ctrl.get_ready(1, 0):
            break
        time.sleep(0.05)
    assert ctrl.get_ready(1, 0) is not None

    ctrl.seek_to_word(1, 0, offset_ms=500)
    gen_before = ctrl._generation
    ctrl.next_page()
    assert ctrl._generation == gen_before + 1
    assert ctrl.word_seek_offset_ms(1, 0) == 0, "a seek must not leave a stale word offset behind"
    assert ctrl.get_ready(1, 0) is not None, "page turn must not discard already-synthesized audio"

    # a re-segmentation, on the other hand, makes every (page, index) mean
    # something else — those results have to go
    ctrl.load_pdf(pdf_path, converter_name="pymupdf4llm")
    assert ctrl._ready == {}


def test_controller_records_synthesis_failure(tmp_dir, pdf_path):
    # A synthesis exception used to vanish into the executor's future: the
    # sentence never became ready and playback just sat silent.
    class BrokenEngine(FakeEngine):
        def synthesize(self, text, out_path):
            if _CALIBRATION_MARKER in text:
                return super().synthesize(text, out_path)
            raise RuntimeError("engine exploded")

    config = {
        "cache": {"dir": tmp_dir, "enabled": False},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 1, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, BrokenEngine(), "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)
    for _ in range(40):
        if ctrl.error_for(1, 0):
            break
        time.sleep(0.05)
    assert "engine exploded" in (ctrl.error_for(1, 0) or "")
    assert not ctrl.is_pending(1, 0), "a failed task must not stay marked pending forever"
    # surfaced as an id list for the client to mark, not baked into the panel
    _pending, failed = ctrl.marked_sentence_ids()
    assert "sent-1-0" in failed.split(",")


def test_controller_survives_dead_engine_at_startup(tmp_dir):
    # Calibration runs a real synthesis at construction; a dead network or a
    # broken local model must surface as a warning, not stop the app booting.
    class DeadEngine(TTSEngine):
        def synthesize(self, text, out_path):
            raise ConnectionError("no route to host")

    config = {
        "cache": {"dir": tmp_dir, "enabled": False},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 1, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, DeadEngine(), "fake", "v1", is_local_engine=True)
    assert ctrl.rtf_warning and "not responding" in ctrl.rtf_warning


def test_controller_clears_old_converter_cache_on_switch(tmp_dir, pdf_path):
    config = {
        "cache": {"dir": tmp_dir, "enabled": True},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 1, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, FakeEngine(), "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)
    old_cache_dir = ctrl.cache.dir
    assert os.path.isdir(old_cache_dir)

    ctrl.load_pdf(pdf_path, converter_name="pymupdf4llm")
    assert not os.path.exists(old_cache_dir), "old converter's cache must be removed after switching"
    assert os.path.isdir(ctrl.cache.dir)
    assert ctrl.cache.dir != old_cache_dir


def test_controller_cache_hit_is_fast(tmp_dir, pdf_path):
    config = {
        "cache": {"dir": tmp_dir, "enabled": True},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 1, "split_long_sentences_over_chars": 1000},
    }
    engine = FakeEngine(delay_s=0.2)
    ctrl = PlaybackController(config, engine, "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)
    ctrl.play()
    for _ in range(30):
        if ctrl.get_ready(1, 0):
            break
        time.sleep(0.05)
    assert ctrl.get_ready(1, 0) is not None

    ctrl.seek_to_sentence(1, 0)
    start = time.monotonic()
    for _ in range(30):
        if ctrl.get_ready(1, 0):
            break
        time.sleep(0.02)
    elapsed = time.monotonic() - start
    assert elapsed < 0.2, f"cache-hit replay should be near-instant, took {elapsed}s"


def test_controller_next_position_crosses_page_boundary(tmp_dir, pdf_path):
    # next_position() is what feeds the frontend's gapless handoff — if it
    # stalls at the last sentence of a page, playback goes silent on every
    # page turn instead of only at the end of the document.
    config = {
        "cache": {"dir": tmp_dir, "enabled": False},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 1, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, FakeEngine(), "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)

    assert ctrl.next_position() == (1, 1)

    last_page = ctrl.page_count
    ctrl.seek_to_sentence(last_page, len(ctrl.sentences_for(last_page)) - 1)
    assert ctrl.next_position() is None  # end of document, nothing to hand off to

    if last_page > 1:
        ctrl.seek_to_sentence(1, len(ctrl.sentences_for(1)) - 1)
        assert ctrl.next_position() == (2, 0)


def test_word_wrapping_preserves_text_and_markup():
    # Word spans are rendered up front, before any audio exists, so the panel
    # never changes shape mid-playback. They must not disturb the text or the
    # inline markup around them.
    for raw in [
        "This is **two words** bold and *italic* here.",
        "See [the link](http://x.com) and `code_here`, ok?",
        'He said, "it works" (mostly); we shipped it.',
    ]:
        rendered = render_inline_html(raw)
        wrapped = wrap_words_in_html(rendered)
        assert re.sub(r"<[^>]+>", "", wrapped) == re.sub(r"<[^>]+>", "", rendered), wrapped
        # Indices must count *spoken* words, so they line up with
        # align_timings_to_words. A tag boundary inside a word ("<code>x</code>,")
        # yields two spans, and both must share one index rather than consume two.
        indices = re.findall(r'class="word" data-w="(\d+)"', wrapped)
        assert len(set(indices)) == len(re.sub(r"<[^>]+>", "", rendered).split()), wrapped
        assert [int(i) for i in sorted(set(indices), key=int)] == list(range(len(set(indices))))

    # emphasis spanning two words nests the spans rather than being split
    wrapped = wrap_words_in_html(render_inline_html("a **two words** b"))
    assert "<strong><span" in wrapped and "</span></strong>" in wrapped


def test_align_timings_to_words():
    from models import WordTiming

    def timings(*pairs):
        return [WordTiming(word=w, start_ms=s, end_ms=s + 90) for w, s in pairs]

    text = 'He said, "it works" (mostly); we shipped it.'
    words = text.split()  # He / said, / "it / works" / (mostly); / we / shipped / it.
    aligned = PlaybackController.align_timings_to_words(
        text,
        timings(("He", 0), ("said", 100), ("it", 200), ("works", 300), ("mostly", 400),
                ("we", 500), ("shipped", 600), ("it", 700)),
    )
    assert len(aligned) == len(words)
    assert aligned[0] == [0, 90]  # punctuation rides along with its word
    assert aligned[1] == [100, 190]
    # the repeated "it" must land on its *own* occurrence, not the first match
    assert aligned[2] == [200, 290]
    assert aligned[7] == [700, 790]

    # several boundaries for one visible token keep the union of their span
    merged = PlaybackController.align_timings_to_words(
        "a well-known result", timings(("a", 0), ("well", 100), ("known", 200), ("result", 300))
    )
    assert merged == [[0, 90], [100, 290], [300, 390]]

    # a timing the engine normalized away is skipped, leaving its word untimed
    # — it can no longer corrupt the text, since this returns indices not markup
    partial = PlaybackController.align_timings_to_words(
        "we saw 42 cases", timings(("we", 0), ("saw", 100), ("forty-two", 200), ("cases", 300))
    )
    assert partial == [[0, 90], [100, 190], None, [300, 390]]


def test_panel_html_is_independent_of_playback_state(tmp_dir, pdf_path):
    # The whole point of the render/state split: one sentence finishing
    # prefetch must not change a single byte of the panel, or Gradio remounts
    # the entire subtree and takes the live highlight with it.
    config = {
        "cache": {"dir": tmp_dir, "enabled": False},
        "pdf": {"render_dpi": 72, "strip_repeated_headers_footers": False, "converter": "plain"},
        "playback": {"buffer_ahead": 2, "split_long_sentences_over_chars": 1000},
    }
    ctrl = PlaybackController(config, FakeEngine(delay_s=0.05), "fake", "v1", is_local_engine=True)
    ctrl.load_pdf(pdf_path)
    before = ctrl.render_text_panel_html()
    assert 'class="word"' in before, "word spans must exist before any audio does"
    assert "data-pending" not in before and "data-error" not in before

    ctrl.play()
    for _ in range(40):
        if ctrl.get_ready(1, 0):
            break
        time.sleep(0.05)
    assert ctrl.get_ready(1, 0) is not None
    assert ctrl.render_text_panel_html() == before, "panel changed when audio became ready"

    # the state that used to live in that HTML is published separately
    pending, failed = ctrl.marked_sentence_ids()
    assert failed == ""
    assert "sent-1-0" not in pending  # already ready

    # a page turn is the one thing that legitimately re-renders
    ctrl.next_page()
    assert ctrl.render_text_panel_html() != before


def run_all():
    # two tests deliberately break the engine; their logged tracebacks are
    # expected output and would otherwise bury the actual test results
    logging.getLogger("playback.controller").setLevel(logging.CRITICAL)

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = os.path.join(tmp_dir, "sample.pdf")
        make_sample_pdf(pdf_path)

        test_segmentation()
        print("ok: segmentation")
        test_segmentation_ignores_line_wraps()
        print("ok: segmentation ignores line wraps")
        test_segmentation_et_al_is_not_a_sentence_break()
        print("ok: segmentation handles 'et al.' correctly")
        test_segmentation_markdown_blocks()
        print("ok: segmentation markdown blocks")
        test_strip_html_tags()
        print("ok: strip html tags, keep content")
        test_inline_markdown()
        print("ok: inline markdown strip + render")
        test_inline_markdown_passes_do_not_corrupt_each_other()
        print("ok: inline markdown passes don't corrupt each other")
        test_word_wrapping_preserves_text_and_markup()
        print("ok: word wrapping preserves text and markup")
        test_align_timings_to_words()
        print("ok: timings align to word indices")
        test_plain_text_literal_markup_chars_survive_intact()
        print("ok: plain-text literal markup chars survive intact")
        test_chunking()
        print("ok: chunking")
        test_cache_roundtrip(os.path.join(tmp_dir, "cache1"))
        print("ok: cache roundtrip")
        test_cache_miss_when_sentence_text_changed(os.path.join(tmp_dir, "cache1b"))
        print("ok: cache entry invalidated when sentence text changes")
        test_legacy_cache_entry_is_kept_when_its_word_timings_still_match(os.path.join(tmp_dir, "cache1c"))
        print("ok: legacy cache entry kept when word timings still match")
        test_controller_seek_bumps_generation_but_keeps_ready_audio(
            os.path.join(tmp_dir, "cache2"), pdf_path
        )
        print("ok: seek bumps generation but keeps ready audio")
        test_controller_records_synthesis_failure(os.path.join(tmp_dir, "cache2a"), pdf_path)
        print("ok: synthesis failure recorded and surfaced")
        test_controller_survives_dead_engine_at_startup(os.path.join(tmp_dir, "cache2d"))
        print("ok: dead engine at startup degrades to a warning")
        test_controller_next_position_crosses_page_boundary(os.path.join(tmp_dir, "cache2b"), pdf_path)
        print("ok: next_position crosses page boundary, stops at document end")
        test_panel_html_is_independent_of_playback_state(os.path.join(tmp_dir, "cache2e"), pdf_path)
        print("ok: panel html independent of playback state")
        test_controller_cache_hit_is_fast(os.path.join(tmp_dir, "cache3"), pdf_path)
        print("ok: cache-hit replay is fast")
        test_controller_clears_old_converter_cache_on_switch(os.path.join(tmp_dir, "cache4"), pdf_path)
        print("ok: switching converter clears old cache")

    print("ALL TESTS PASSED")


if __name__ == "__main__":
    run_all()
