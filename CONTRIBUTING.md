# Contributing to PDF Immersive Reading

Thanks for considering a contribution! This is a small, local-first project, so the
process is intentionally lightweight.

## Getting set up

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-optional.txt   # only if you're touching an optional backend
```

Run the app locally with `.venv/bin/python app.py`.

## Running tests

```
.venv/bin/python tests/test_pipeline.py
```

This is a single script using plain asserts (not pytest) and substitutes a fake
in-process TTS engine, so it makes no network calls. Please make sure it passes
before opening a PR. If you add a feature, add or extend a case in this file
rather than introducing a new test runner unless there's a good reason to.

## Code style

- Follow the existing structure: TTS backends live behind the `TTSEngine`
interface in `engines/`, PDF backends behind `PDFConverter` in `pdf/converters/`.
New backends should implement the existing interface rather than special-casing
logic elsewhere in the app.
- Keep the default install light. If your change needs a new dependency, put it
in `requirements-optional.txt` and import it lazily (only when that
engine/converter is actually selected), matching the pattern used by Kokoro,
XTTS, Docling, marker, and MarkItDown.
- Prefer clear, small functions over cleverness — this is a single-maintainer
project meant to stay easy to read.
- CI runs `ruff check .`. Run it locally before pushing:
  ```
  pip install ruff
  ruff check .
  ```

## Making changes

1. Fork the repo and create a branch off `master`.
2. Make your change, with tests updated/added where relevant.
3. Run the test script and `ruff` locally.
4. Open a PR with a short description of *why*, not just *what* — especially for
anything touching playback timing, caching, or the segmenter, since those are
the trickiest parts of the codebase to get right.

## What's especially welcome

- New TTS engine or PDF converter backends (as long as they follow the existing
interfaces and stay opt-in).
- Fixes for the known limitations listed in the README (e.g. sentences that span
page breaks, header/footer stripping for non-`plain` converters).
- Documentation and Docker/packaging improvements.

## What to discuss first

Larger architectural changes (e.g. to `playback/controller.py`'s prefetch/cache
state machine or the highlighting mechanism in `frontend/highlight.js`) are
easier to get right with a quick design discussion first — please open an issue
before investing a lot of time in a large PR.

## Reporting bugs / requesting features

Open a GitHub issue. For bugs, include your OS, Python version, which TTS engine
and PDF converter you were using, and steps to reproduce.

## Code of conduct

Be respectful and constructive. That's it.