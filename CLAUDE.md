# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Setup (default: edge-tts + plain/pymupdf4llm converters, no ML models)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Optional extras (Kokoro/XTTS engines, MarkItDown/Docling/marker converters)
.venv/bin/pip install -r requirements-optional.txt   # all
.venv/bin/pip install kokoro                          # tts.engine: kokoro
.venv/bin/pip install coqui-tts                        # tts.engine: xtts
.venv/bin/pip install docling                          # converter: docling
.venv/bin/pip install marker-pdf                       # converter: marker
.venv/bin/pip install "markitdown[pdf]"                # converter: markitdown

# Run the app
.venv/bin/python app.py    # http://127.0.0.1:7860

# Run the self-check suite (only test file, no pytest — plain asserts)
.venv/bin/python tests/test_pipeline.py

# Docker (installs requirements.txt only — the lightweight default setup)
docker build -t pdf-active-reader .
docker run -p 7860:7860 -e PORT=7860 pdf-active-reader
```

There is no separate lint/build step. `tests/test_pipeline.py` is a single
runnable script (`if __name__ == "__main__": run_all()`), not pytest-based —
run it directly, not via `pytest`. It uses a fake in-process TTS engine, so
it makes no network calls and runs in a few seconds.

`app.py`'s `demo.launch()` reads `HOST`/`PORT` env vars first, falling back
to `config.yaml`'s `server.host`/`server.port` — this is what lets
`docker run -e PORT=...` pick the port without touching the config file.
The Dockerfile sets `HOST=0.0.0.0` as the container's default (not
`config.yaml`'s own default, which stays whatever the local single-user
setup wants) since `-p` port-mapping can't reach a process bound to the
container's own loopback interface.

## Architecture

Local-only Gradio app (binds `127.0.0.1`, single user, no auth by design)
that reads PDFs aloud sentence-by-sentence with live highlighting. Full
design rationale lives in `pdf-read-aloud-design.md` at the repo root —
read it for the "why" behind the prefetch pipeline and highlighting wiring.

### Two pluggable-backend systems, same pattern

Both **TTS engines** (`engines/`) and **PDF->markdown converters**
(`pdf/converters/`) follow the same shape: an ABC (`TTSEngine` /
`PDFConverter`), one file per backend, heavy/optional ones deferred-import
their dependency inside the method body (not at module top) so the app
still boots if that dependency isn't installed — it only errors when that
specific backend is actually selected. `pdf/converters/registry.py` maps
name -> class for the GUI dropdown. `PDFConverter.__init__` takes
`strip_headers_footers` on the base class so every converter accepts it
uniformly and `build_converter` stays a one-liner — only `plain` acts on it,
the markdown backends do their own layout analysis.

Key asymmetry: **TTS engine is config-only** (`config.yaml`, chosen once at
startup, requires a restart to change — `app.py:build_engine`). The **PDF
converter is a live GUI dropdown** (`app.py`'s `converter_dropdown`) —
switching it re-runs `PlaybackController.reload_with_converter()` against
the currently open PDF immediately, no restart.

`PDFConverter.produces_inline_markdown` (default `True`, `False` for
`plain`) gates whether `**`/`*`/`` ` ``/`[]()` in a converter's output get
interpreted as markdown at all — `plain`'s raw extraction was never
markdown, so a literal asterisk/underscore/bracket there (citation markers,
variable names, footnotes) must survive untouched rather than get stripped
or rendered as bold/italic/code/link. This gates both
`text/segmenter.py:segment_page`'s inline-stripping and
`controller.render_markdown`; a real converter must derive this markup from
actual font-styling metadata (pymupdf4llm/markitdown/docling/marker do —
their `**`/`*` reflect real bold/italic runs in the PDF), never set it True
for a converter whose text is closer to raw extraction.

### `playback/controller.py` — the central state machine

`PlaybackController` is a module-level singleton in `app.py` (not per-session
`gr.State` — background threads/synthesis queue don't serialize across
Gradio sessions anyway, and the app is single-user). It owns:

- **Producer/consumer prefetch**: a `ThreadPoolExecutor` synthesizes
  `buffer_ahead + 1` sentences ahead of playback (§10 of the design doc).
  Worker count is sized to `buffer_ahead + 1` for network-bound engines
  (edge-tts) so a seek's synthesis never queues behind routine prefetch;
  local engines get exactly 1 worker (concurrent inference isn't assumed
  thread-safe).
- **Generation-tagged seeks**: every seek/page-turn bumps `self._generation`
  and clears `_word_seek_offsets`. A queued synthesis task checks its captured
  generation before starting real work (in `_synthesize_one`) so a task made
  stale by a seek bails out fast instead of burning a worker slot.
  `word_seek_offset_ms()` (click-to-seek within a sentence) rides the same
  mechanism.
  - `_bump_generation()` deliberately does **not** clear `_ready`, and
    `_store_result` stores unconditionally. Those entries are a pure memo
    keyed by (page, index) — the audio for sentence (3, 7) is still the right
    audio for (3, 7) after a seek, and a finished clip is worth keeping no
    matter when it landed. Clearing it made every seek and page turn briefly
    silent, because `poll()` emits `data-src=""` for a sentence with no ready
    audio. Only `_reset_queue()` (new PDF / new converter, i.e. the sentence
    list itself changed) may clear it — see the test pair
    `test_controller_seek_bumps_generation_but_keeps_ready_audio`.
  - `_pending` is a dict `(page, index) -> generation`, not a set. That's what
    lets a bump invalidate in-flight work without clearing it: a stale entry
    no longer blocks re-submission, and a stale task's own `finally` can't
    delete the entry belonging to its replacement.
  - `_errors` records `(generation, message)` per position. A synthesis
    exception used to vanish into the executor's future — the sentence just
    never became ready and playback sat silent with nothing explaining why.
    Now it is logged, marked `data-error="1"` in the panel, and shown in the
    warning line; a seek (new generation) is what retries it.
  - Each engine call runs in its own disposable one-off `ThreadPoolExecutor`
    with a hard timeout (`_call_engine`) — a network stall or wedged local
    model call must never permanently shrink the shared worker pool.
- **On-disk cache** (`playback/cache.py`): keyed by
  `pdf_hash + engine + converter + voice`, so switching TTS engine, voice,
  *or* PDF converter never collides with a previous combination's cache —
  they coexist side by side, and cache hits make rewinding effectively
  instant (there's a test asserting this: `test_controller_cache_hit_is_fast`).
  Within one of those directories an entry's *path* is only
  `page_no`/`index_in_page`, so `cache.py` also records the synthesized
  `text` in the sidecar JSON and treats a mismatch as a miss
  (`_is_valid_for`) — a segmenter or converter fix that changes a sentence's
  text (HTML markup no longer spoken, say) leaves the path identical, and
  replaying the old entry would both speak the old text and re-render the
  panel from its stale `word_timings`, putting the old markup back on
  screen. Invalidation must stay *per sentence*: dropping a whole
  document's worth of still-correct audio at once is audible, since
  `poll()` emits `data-src=""` for a sentence that isn't ready yet and
  `highlight.js` then has no audio to play. That's why a sidecar with no
  `text` field (written before it existed) is validated against its own
  `word_timings` instead of discarded — they record what was actually
  voiced *and* are what the panel re-renders from, so agreeing with them
  rules out both symptoms without a re-synthesis.

### Text pipeline (why sentence splitting exists as its own layer)

`pdf/converters/*` produce markdown text per page ->
`text/segmenter.py:segment_page()` splits it into `Sentence` objects, which
is markdown-*block*-aware: it splits into blocks (heading / list-item /
paragraph) first, joins line-wrapped text *within* a block into one string,
and only then runs `pysbd` sentence splitting on that joined text. Running
pysbd directly on raw PDF-extracted text (one hard `\n` per visual line)
makes it split on every line wrap instead of on punctuation — this
block-then-join step is the actual fix for that and must not be reverted.

`Sentence.text` is the cleaned, speakable version (markdown markup
stripped by `text/markdown_inline.py:strip_inline_markdown`) — this is what
gets passed to TTS. `Sentence.display_text` keeps the original markup, used
by `PlaybackController.render_text_panel_html()` to render bold/italic/code/
links via `render_inline_html` for any converter except `plain`
(`controller.render_markdown` property gates this).

`render_inline_html` and `strip_inline_markdown` run their passes through a
`_Stash`: every pass rewrites the whole string, so whatever an earlier pass
emitted is still matchable by a later one. That corrupted real output — the `_`
in a link's own `target="_blank"` paired with any later underscore in the
sentence and swallowed the `<a>` tag, and inline-code content was re-read as
bold/italic. Stashed fragments (whole code spans, link opening tags) sit behind
a private-use-character placeholder until the end. Underscore emphasis also
requires non-word characters on the outside per CommonMark, so
`variable_name_here` isn't italicized — snake_case is common in these PDFs.

### Render/state split — why the panel never re-renders during playback

`render_text_panel_html()` depends on the current page's sentences and
**nothing else**, and is cached per page (`_panel_html_cache`, cleared only by
`_reset_queue`). Playback state used to be baked into it — word spans appeared
only once timings arrived, plus `data-pending`/`data-error` attributes — so one
sentence finishing prefetch rebuilt kilobytes of HTML, Gradio remounted the
subtree, and the live highlight classes on the sentence *currently playing*
were destroyed. Do not reintroduce per-sentence state into this function.

Everything that varies during playback travels as data in the `#rd-control`
div instead, and `highlight.js` applies it to the existing DOM:

- **word spans exist from the first render**, before any audio.
  `wrap_words_in_html()` wraps each visible word of the *already-rendered*
  inline HTML, stepping over tags. It can't wrap the markdown instead —
  splitting that on whitespace cuts delimiters in half (`**two words**` ->
  `**two` + `words**`). Emphasis spanning several words simply nests. A tag
  boundary *inside* a word (`<code>x</code>,`) must produce two spans sharing
  one `data-w` index, since one span can't swallow a tag pair without breaking
  nesting; the client groups spans by index.
- **timings arrive as indices, not markup.** `align_timings_to_words()` maps
  each `WordTiming` onto a `data-w` index via a monotonically advancing char
  cursor (so a repeated word lands on its own occurrence), merging multiple
  boundaries for one token (`well-known` -> `well`, `known`) and skipping a
  timing it can't locate. `word_timings_json()` publishes
  `[[start,end],null,...]`; the client stamps `data-start`/`data-end`. The
  *next* sentence's timings ride along too, so the gapless handoff highlights
  from its first frame instead of a poll interval later.
- **pending/error are id lists** (`marked_sentence_ids()`), toggled as
  attributes client-side.

Both `poll()` outputs are memoized (`_skip_if_unchanged`), so in steady state
the Timer pushes **nothing at all** between sentence boundaries.

### Highlighting is word-level only

There was also a whole-sentence highlight. It was applied once per sentence
from a JS closure variable, so any panel re-render dropped it mid-playback with
nothing to restore it — the failure mode that motivated the split above. Word
highlighting has no such state: it is re-derived from the DOM every frame.

It runs on `requestAnimationFrame` while playing, **not** on `timeupdate` —
the media spec only guarantees timeupdate at ~4Hz and browsers fire it about
every 250ms, while spoken words last 150-400ms, so it lagged by up to a quarter
second and skipped short words. `paintFrame` keeps a forward-only scan cursor
and a cached active group, so a frame where nothing changed touches no DOM.

### Frontend wiring (`app.py` + `frontend/highlight.js`)

The text panel is one `gr.HTML` block re-rendered by a `gr.Timer`
(`POLL_INTERVAL_S`) calling `poll()`. The actual `<audio>` element is created
**once** by `highlight.js` (injected together with `frontend/styles.css` via
`gr.Blocks.launch(head=...)` — real `<script>`/`<style>` tags, unlike ones set
via innerHTML, which browsers won't execute) and lives
outside Gradio's reactive re-render cycle, so repeated HTML updates never
restart playback. `highlight.js` polls a small `#rd-control` signal div
(sentence id, generation, audio src, seek offset) on its own interval and
reacts to *changes* in sentence id / generation / src — this is what lets a
same-sentence re-seek (new generation, same sentence id) still reload
correctly.

Two things keep playback from stuttering, and both matter:

- `poll()` returns `gr.skip()` for the text panel when the rendered HTML is
  byte-identical to what it last emitted (`_last_text_html`). The panel is a
  pure function of controller state, so most 300ms ticks produce the same
  string; pushing it anyway makes Gradio replace the whole subtree ~3x/second,
  which flashes and drops the live `.active-sentence`/`.active-word` classes
  until `highlight.js` re-applies them. `demo.load(_reset_panel_cache)` clears
  that memo, since a browser reload mounts an empty panel while the memo still
  holds the previous page's HTML.
- `poll()` also publishes the *next* sentence's already-synthesized audio URL
  as `data-nextsrc`/`data-nextsentid`, and `highlight.js`'s `ended` handler
  starts it immediately instead of waiting out `ended` -> `#rd-advance-btn` ->
  Python -> Timer tick -> its own 250ms tick (~0.5s of silence per sentence).
  It still clicks the advance button so the server's position follows; the
  server's next poll then reports exactly that sentence id + src, so `tick()`
  sees no change and won't restart the clip. Consequently `tick()` only calls
  `removeAttribute("src")` on an empty `data-src` **when the audio is paused** —
  an empty src mid-playback just means the server hasn't caught up with the
  handoff yet.

Hidden buttons (`elem_classes=["rd-hidden"]`, CSS `display:none` — **not**
Gradio's `visible=False`, which unmounts the component from the DOM
entirely) bridge JS events back to Python: `#rd-advance-btn` (audio `ended`
-> auto-advance) and `#rd-seek-btn` + `#rd-seek-payload` (word/sentence
click -> `on_seek`, which sets a JSON payload textbox then clicks the
button). `on_seek` deliberately takes no Gradio outputs of its own — giving
it outputs that overlapped the Timer's caused a race where the seek's own
render got silently dropped until some later, unrelated event fired; letting
the Timer's own next tick pick up the state avoids that.

Handlers all end by publishing the same state through `poll()` or
`_poll_with_image()`; the `PANEL_OUTPUTS` / `IMAGE_OUTPUTS` lists inside the
`Blocks` are matched to those two arities, so a handler's return tuple can't
drift out of step with its `outputs=` (which fails silently at runtime).

`/gradio_api/file=<path>` is Gradio 6's static file route (not `/file=`,
which was the old route in earlier Gradio versions) — used both for serving
synthesized audio and page images, gated by `allowed_paths=[cache_dir_abs]`
passed to `launch()`.
