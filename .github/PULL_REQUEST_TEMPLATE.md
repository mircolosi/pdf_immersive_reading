## What does this change do, and why?

<!-- Focus on the "why" — especially for anything touching playback timing,
     caching, or the segmenter, since those are the trickiest parts to get right. -->

## Type of change

- [ ] Bug fix
- [ ] New TTS engine or PDF converter backend
- [ ] Other new feature
- [ ] Documentation
- [ ] Refactor / internal cleanup

## Checklist

- [ ] `.venv/bin/python tests/test_pipeline.py` passes
- [ ] `ruff check .` passes (or pre-existing issues only)
- [ ] I added/updated a test case if this changes behavior
- [ ] If this adds a dependency, it's in `requirements-optional.txt` and imported
      lazily (only when that engine/converter is selected)
- [ ] If this adds a backend, it implements the existing `TTSEngine` /
      `PDFConverter` interface
- [ ] I updated the README (features table, engines/converters table, or
      limitations) if this changes user-facing behavior

## How was this tested?

<!-- Manual steps, PDFs/engines/converters you tried, platform (OS, GPU/CPU), etc. -->

## Related issues

<!-- Closes #123, or "N/A" -->
