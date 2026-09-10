// Runs once as a real <script> tag (injected via gr.Blocks(head=...), so it
// actually executes — unlike a <script> set through innerHTML). Creates one
// persistent <audio> element that outlives Gradio's HTML re-renders, and
// polls a small "#rd-control" signal div (re-rendered every tick by the
// Python side) to know what the audio element should be doing. See design
// doc §11.
(function () {
  if (window.__rdHighlightInit) return; // guards against Gradio injecting this script twice
  window.__rdHighlightInit = true;

  let lastSentId = null;
  let lastGen = null;
  let lastSrc = null;
  let lastAction = null;

  // Playback speed: post-processing on already-synthesized audio via the
  // browser's native <audio>.playbackRate — never a TTS parameter, so it
  // applies uniformly across engines and doesn't touch cached audio files.
  // Set by the speed button set's client-side-only JS (see app.py); persists
  // across sentences (playbackRate isn't reset by changing .src), but we
  // reapply it defensively on every reload below in case a browser resets it.
  window.__rdPlaybackRate = window.__rdPlaybackRate || 1;

  // ---- word highlighting -------------------------------------------------
  // The panel's word spans are rendered once per page and never rebuilt. All
  // that arrives during playback is a timings array from the control div,
  // which gets stamped onto those existing spans as data-start/data-end.

  let paintHandle = null;
  let activeGroup = null;
  // the current sentence's timed words, in order. One entry per spoken word;
  // its `els` holds the one or more spans that word was split into (an inline
  // tag boundary mid-word produces two spans sharing one data-w index).
  let paintedWords = [];
  let scanFrom = 0; // words play left to right; resume the scan where it stopped

  function clearActiveWord() {
    if (activeGroup) activeGroup.els.forEach((el) => el.classList.remove("active-word"));
    activeGroup = null;
  }

  function stampTimings(sentId, timingsJson) {
    if (!sentId || !timingsJson) return false;
    const span = document.getElementById(sentId);
    if (!span) return false;
    let timings;
    try {
      timings = JSON.parse(timingsJson);
    } catch (e) {
      return false;
    }
    span.querySelectorAll(".word").forEach((el) => {
      const t = timings[+el.dataset.w];
      if (!t) return;
      el.dataset.start = t[0];
      el.dataset.end = t[1];
    });
    return true;
  }

  function loadSentenceWords(sentId) {
    clearActiveWord();
    scanFrom = 0;
    paintedWords = [];
    const span = sentId ? document.getElementById(sentId) : null;
    if (!span) return; // no timings yet (or Kokoro/XTTS, which have none)
    const byIndex = new Map();
    span.querySelectorAll(".word[data-start]").forEach((el) => {
      const group = byIndex.get(el.dataset.w);
      if (group) {
        group.els.push(el);
        return;
      }
      byIndex.set(el.dataset.w, {
        start: parseFloat(el.dataset.start),
        end: parseFloat(el.dataset.end),
        els: [el],
      });
    });
    paintedWords = Array.from(byIndex.values()).sort((a, b) => a.start - b.start);
  }

  function paintFrame() {
    paintHandle = requestAnimationFrame(paintFrame);
    if (!paintedWords.length) return;
    const audio = document.getElementById("rd-player");
    if (!audio) return;
    const ms = audio.currentTime * 1000;

    // A seek can move backwards, so restart the scan when the cursor is behind
    // where we left off; otherwise walk forward from there. That keeps the
    // per-frame cost at one or two comparisons rather than rescanning every
    // word of the sentence 60 times a second.
    if (scanFrom > 0 && ms < paintedWords[scanFrom].start) scanFrom = 0;
    let hit = null;
    for (let i = scanFrom; i < paintedWords.length; i++) {
      const group = paintedWords[i];
      if (ms < group.start) break;
      scanFrom = i;
      if (ms < group.end) {
        hit = group;
        break;
      }
    }
    if (hit === activeGroup) return; // same word as last frame: touch nothing
    clearActiveWord();
    if (hit) {
      hit.els.forEach((el) => el.classList.add("active-word"));
      activeGroup = hit;
    }
  }

  function startPainting() {
    if (paintHandle === null) paintFrame();
  }

  function stopPainting() {
    if (paintHandle !== null) cancelAnimationFrame(paintHandle);
    paintHandle = null;
  }

  // Pending/failed markers used to be attributes baked into the panel HTML, so
  // one sentence finishing prefetch re-rendered the whole page. Now they are
  // published as id lists and toggled here.
  let lastMarkers = "";

  function applyMarkers(pendingIds, errorIds) {
    const signature = pendingIds + "|" + errorIds;
    if (signature === lastMarkers) return;
    lastMarkers = signature;
    document
      .querySelectorAll(".sentence[data-pending], .sentence[data-error]")
      .forEach((el) => {
        el.removeAttribute("data-pending");
        el.removeAttribute("data-error");
      });
    const mark = (ids, attr) => {
      ids.split(",").forEach((id) => {
        if (!id) return;
        const el = document.getElementById(id);
        if (el) el.setAttribute(attr, "1");
      });
    };
    mark(pendingIds, "data-pending");
    mark(errorIds, "data-error");
  }

  function ensurePlayer() {
    let audio = document.getElementById("rd-player");
    if (!audio) {
      audio = document.createElement("audio");
      audio.id = "rd-player";
      audio.style.display = "none";
      audio.playbackRate = window.__rdPlaybackRate;
      document.body.appendChild(audio);

      // Word highlighting runs on requestAnimationFrame, not on "timeupdate".
      // The media spec only guarantees timeupdate at ~4Hz and browsers fire it
      // about every 250ms, while spoken words last 150-400ms — so it lagged by
      // up to a quarter second and skipped short words entirely.
      audio.addEventListener("play", startPainting);
      audio.addEventListener("pause", stopPainting);

      audio.addEventListener("ended", () => {
        stopPainting();
        clearActiveWord();

        // Gapless handoff: the server publishes the *next* sentence's audio
        // URL as soon as prefetch has it, so start it right here instead of
        // waiting out ended -> advance-btn -> Python -> Timer tick -> our own
        // tick (~0.5s of silence between every sentence). The advance click
        // below still moves the server's own position; its next poll then
        // reports exactly this sentence id + src, so tick() sees no change and
        // won't restart the clip we already started.
        const control = document.getElementById("rd-control");
        const nextSrc = control ? control.dataset.nextsrc : "";
        const nextSentId = control ? control.dataset.nextsentid : "";
        if (nextSrc && nextSentId) {
          lastSentId = nextSentId;
          lastSrc = nextSrc;
          // the next sentence's timings ride along in the same div, so its
          // words highlight from the first frame rather than after the server
          // catches up one poll interval later
          stampTimings(nextSentId, control.dataset.nexttimings);
          loadSentenceWords(nextSentId);
          audio.src = nextSrc;
          audio.playbackRate = window.__rdPlaybackRate;
          audio.play().catch(() => {});
        }

        const btn = document.querySelector("#rd-advance-btn button, #rd-advance-btn");
        if (btn) btn.click();
      });
    }
    return audio;
  }

  function tick() {
    onZoomScroll(); // redundant safety net alongside the scroll listener below
    const control = document.getElementById("rd-control");
    if (!control) return;
    const audio = ensurePlayer();

    const sentId = control.dataset.sentid || null;
    const gen = control.dataset.gen || null;
    const action = control.dataset.action || "pause";
    const src = control.dataset.src || "";
    const seekMs = parseFloat(control.dataset.seekms || "0");

    applyMarkers(control.dataset.pendingids || "", control.dataset.errorids || "");

    // Timings for the current sentence may land after its audio did (or after
    // a panel re-render dropped the stamps), so re-stamp until it takes. Cheap
    // and idempotent: writing the same data-start values changes nothing.
    if (stampTimings(sentId, control.dataset.timings) && !paintedWords.length) {
      loadSentenceWords(sentId);
    }

    // Reload whenever the sentence changes (natural advance), the generation
    // changes (any seek/page-turn, including a re-seek into the *same*
    // sentence from a word click), or src itself newly appears (buffering
    // finished — sentId/gen may not have changed at all since we started
    // waiting on this sentence's audio).
    if (sentId !== lastSentId || gen !== lastGen || src !== lastSrc) {
      const sentenceChanged = sentId !== lastSentId;
      lastSentId = sentId;
      lastGen = gen;
      lastSrc = src;
      if (sentenceChanged) loadSentenceWords(sentId);
      if (src) {
        audio.src = src;
        audio.playbackRate = window.__rdPlaybackRate;
        const applySeek = () => {
          if (seekMs > 0) audio.currentTime = seekMs / 1000;
        };
        if (audio.readyState >= 1) applySeek();
        else audio.addEventListener("loadedmetadata", applySeek, { once: true });
        if (action === "play") audio.play().catch(() => {});
      } else if (audio.paused) {
        audio.removeAttribute("src");
      }
      // src === "" while audio is still playing means the gapless handoff above
      // already started this sentence and the server just hasn't caught up yet
      // — yanking the source out mid-clip is exactly the interruption we're
      // fixing, so leave it alone.
    } else if (action !== lastAction) {
      if (action === "play" && src && audio.paused) audio.play().catch(() => {});
      if (action === "pause" && !audio.paused) audio.pause();
    }
    lastAction = action;
  }

  // Click-to-seek: clicking a word (edge-tts) jumps to its timestamp;
  // clicking a plain sentence (Kokoro/XTTS, no word spans) jumps to its
  // start. Routed through the server via a hidden textbox + button since
  // seeking may need a fresh synthesis (or a cache hit) before it can play.
  function setNativeValue(el, value) {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value").set;
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function onTextPanelClick(e) {
    const sentenceSpan = e.target.closest(".sentence");
    if (!sentenceSpan) return;
    const match = sentenceSpan.id.match(/^sent-(\d+)-(\d+)$/);
    if (!match) return;
    // word spans exist before their timings do, so dataset.start can be
    // absent — fall back to the sentence start rather than sending NaN
    const wordSpan = e.target.closest(".word[data-start]");
    const offsetMs = wordSpan ? parseFloat(wordSpan.dataset.start) : 0;

    const payloadBox = document.querySelector("#rd-seek-payload textarea, #rd-seek-payload input");
    const seekBtn = document.querySelector("#rd-seek-btn button, #rd-seek-btn");
    if (!payloadBox || !seekBtn) return;
    setNativeValue(payloadBox, JSON.stringify({ page: parseInt(match[1], 10), idx: parseInt(match[2], 10), offset_ms: Math.round(offsetMs) }));
    seekBtn.click();
  }

  document.addEventListener("click", onTextPanelClick);

  // Zoom lens on the page image: click to toggle it on (centered where you
  // clicked), then it tracks the mouse in real time until you click again
  // (anywhere) to dismiss it. Delegated on document rather than bound to the
  // <img> directly, since Gradio replaces that element wholesale on every
  // page turn (a direct listener would be lost silently).
  const ZOOM_FACTOR = 1.5;
  let zoomActive = false;
  let lastMouseX = 0;
  let lastMouseY = 0;

  function ensureZoomLens() {
    let lens = document.getElementById("rd-zoom-lens");
    if (!lens) {
      lens = document.createElement("div");
      lens.id = "rd-zoom-lens";
      document.body.appendChild(lens);
    }
    return lens;
  }

  function updateZoomLens(img, clientX, clientY) {
    const lens = ensureZoomLens();
    const rect = img.getBoundingClientRect();
    const x = Math.min(Math.max(clientX - rect.left, 0), rect.width);
    const y = Math.min(Math.max(clientY - rect.top, 0), rect.height);
    const w = lens.offsetWidth || 240;
    const h = lens.offsetHeight || 320;
    lens.style.backgroundImage = `url("${img.src}")`;
    lens.style.backgroundSize = `${rect.width * ZOOM_FACTOR}px ${rect.height * ZOOM_FACTOR}px`;
    lens.style.backgroundPosition = `${-(x * ZOOM_FACTOR - w / 2)}px ${-(y * ZOOM_FACTOR - h / 2)}px`;
    lens.style.left = `${clientX - w / 2}px`;
    lens.style.top = `${clientY - h / 2}px`;
  }

  function onZoomClick(e) {
    const img = e.target.closest("#rd-page-image img");
    if (img) {
      zoomActive = !zoomActive;
      ensureZoomLens().style.display = zoomActive ? "block" : "none";
      if (zoomActive) updateZoomLens(img, e.clientX, e.clientY);
      return;
    }
    if (zoomActive) {
      zoomActive = false;
      ensureZoomLens().style.display = "none";
    }
  }

  function onZoomMouseMove(e) {
    lastMouseX = e.clientX;
    lastMouseY = e.clientY;
    if (!zoomActive) return;
    const img = document.querySelector("#rd-page-image img");
    if (!img) return;
    updateZoomLens(img, e.clientX, e.clientY);
  }

  // A manual scroll moves the page image under a lens that's sitting still
  // (no new mousemove), so its background-position goes stale relative to
  // the image's new on-screen rect until the mouse moves again. Resync on
  // scroll (capture: the shift can happen on any nested scrollable container,
  // not just window) using the last known pointer position.
  function onZoomScroll() {
    if (!zoomActive) return;
    const img = document.querySelector("#rd-page-image img");
    if (!img) return;
    updateZoomLens(img, lastMouseX, lastMouseY);
  }

  window.addEventListener("scroll", onZoomScroll, true);

  document.addEventListener("click", onZoomClick);
  document.addEventListener("mousemove", onZoomMouseMove);

  setInterval(tick, 250);
})();
