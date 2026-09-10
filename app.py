"""Gradio entrypoint — wires PDF/text/TTS/playback modules together. §3, §11."""
import argparse
import json
import logging
import os
from html import escape

import gradio as gr

from engines.edge_tts_engine import EdgeTTSEngine
from pdf.converters.registry import CONVERTERS
from playback.controller import PlaybackController
from utils.config import load_config

# Synthesis runs on background threads whose exceptions are otherwise
# invisible; without a configured handler those log records are dropped and a
# failed sentence just looks like silence.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# httpx logs a line per request at INFO; Gradio's startup analytics calls would
# otherwise be the loudest thing in the log.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))


def build_engine(config: dict):
    """Engine choice is read once from config.yaml at startup — no runtime
    switching (requirement #6). Local engines load their model once here."""
    name = config["tts"]["engine"]
    if name == "edge-tts":
        cfg = config["tts"]["edge_tts"]
        return EdgeTTSEngine(voice=cfg["voice"], rate=cfg["rate"], pitch=cfg["pitch"]), cfg["voice"], False
    if name == "kokoro":
        from engines.kokoro_engine import KokoroEngine

        cfg = config["tts"]["kokoro"]
        return KokoroEngine(voice=cfg["voice"], device=cfg["device"]), cfg["voice"], True
    if name == "xtts":
        from engines.xtts_engine import XTTSEngine

        cfg = config["tts"]["xtts"]
        return XTTSEngine(device=cfg["device"], speaker=cfg["speaker"]), cfg["speaker"], True
    raise ValueError(f"Unknown tts.engine in config.yaml: {name!r}")


config = load_config(os.path.join(_HERE, "config.yaml"))
engine, voice, is_local = build_engine(config)
engine_name = config["tts"]["engine"]
controller = PlaybackController(config, engine, engine_name, voice, is_local)

cache_dir_abs = os.path.abspath(config["cache"]["dir"])
os.makedirs(cache_dir_abs, exist_ok=True)

def _read_frontend(filename: str) -> str:
    with open(os.path.join(_HERE, "frontend", filename), "r", encoding="utf-8") as f:
        return f.read()


# Injected as real <style>/<script> tags via launch(head=...) — a <script> set
# through innerHTML would never execute.
_HEAD = f"<style>{_read_frontend('styles.css')}</style>\n<script>{_read_frontend('highlight.js')}</script>"

# Playback-rate choices for the speed dropdown; applied client-side via
# <audio>.playbackRate (never a TTS parameter).
SPEED_TICKS = [0.5, 1.0, 1.25, 1.5, 1.75, 2.0]

# How often the server re-publishes playback state. highlight.js polls the
# resulting #rd-control div on its own, slightly shorter interval; sentence
# handoff no longer depends on either (see its "ended" listener), so this only
# bounds how fast a *server-side* change becomes visible.
POLL_INTERVAL_S = 0.3

_SPEED_CHANGE_JS = (
    "(r) => { r = parseFloat(r); "
    "window.__rdPlaybackRate = r; "
    "const a = document.getElementById('rd-player'); if (a) a.playbackRate = r; }"
)


# Both outputs are pure functions of controller state, so most of the Timer's
# ticks produce byte-identical strings. Pushing one anyway makes Gradio replace
# that component's DOM subtree — for the text panel that means remounting
# kilobytes of HTML ~3x/second and destroying the live highlight classes on it.
# Emit gr.skip() per output when nothing changed; in steady state that means
# the timer pushes nothing at all between sentence boundaries.
_last_pushed: dict[str, str | None] = {"panel": None, "control": None}


def _reset_panel_cache():
    """A browser reload mounts empty components while `_last_pushed` still
    holds what the *previous* page showed, so the next tick would skip the very
    update that refills them."""
    _last_pushed["panel"] = None
    _last_pushed["control"] = None


def _skip_if_unchanged(slot: str, value: str):
    if _last_pushed[slot] == value:
        return gr.skip()
    _last_pushed[slot] = value
    return value


def _audio_src(page: int, idx: int) -> str:
    result = controller.get_ready(page, idx)
    return f"/gradio_api/file={result.audio_path}" if result else ""


def poll():
    sentence = controller.current_sentence()
    panel_out = _skip_if_unchanged("panel", controller.render_text_panel_html())

    if sentence is None:
        control = '<div id="rd-control" data-action="pause" data-sentid="" data-src=""></div>'
    else:
        page, idx = sentence.page_no, sentence.index_in_page
        # The next sentence's audio *and* its word timings ride along, so the
        # client's gapless handoff can start the clip and highlight its words
        # without waiting a poll interval for the server to catch up.
        nxt = controller.next_position()
        next_src, next_sent_id, next_timings = "", "", ""
        if nxt and controller.get_ready(*nxt):
            next_src = _audio_src(*nxt)
            next_sent_id = f"sent-{nxt[0]}-{nxt[1]}"
            next_timings = controller.word_timings_json(*nxt)
        pending_ids, error_ids = controller.marked_sentence_ids()

        control = (
            '<div id="rd-control"'
            f' data-action="{"play" if controller.state == "PLAYING" else "pause"}"'
            f' data-sentid="sent-{page}-{idx}"'
            f' data-gen="{controller.generation}"'
            f' data-src="{_audio_src(page, idx)}"'
            f' data-seekms="{controller.word_seek_offset_ms(page, idx)}"'
            f' data-timings="{escape(controller.word_timings_json(page, idx), quote=True)}"'
            f' data-nextsrc="{next_src}"'
            f' data-nextsentid="{next_sent_id}"'
            f' data-nexttimings="{escape(next_timings, quote=True)}"'
            f' data-pendingids="{pending_ids}"'
            f' data-errorids="{error_ids}"'
            "></div>"
        )
    control_html = _skip_if_unchanged("control", control)

    page_label = f"Page {controller.current_page} / {controller.page_count}"
    error = controller.error_for(sentence.page_no, sentence.index_in_page) if sentence else None
    warning = " | ".join(
        w
        for w in (
            controller.rtf_warning,
            controller.converter_warning,
            f"⚠️ Synthesis failed for this sentence — {error}" if error else None,
        )
        if w
    )
    return panel_out, control_html, page_label, warning


# Every transport handler ends by publishing the same state; the only
# difference is whether the page image changed too. These two wrappers are what
# the shared PANEL_OUTPUTS / IMAGE_OUTPUTS lists below are matched against, so
# a handler's return arity can't drift out of step with its `outputs=`.
def _poll_with_image():
    image_path = controller.page_image_path(controller.current_page) if controller.pdf_path else None
    return (image_path, *poll())


def on_pdf_uploaded(file, converter_name):
    if file is None:
        return None, "", "", "Page 0 / 0", ""
    controller.load_pdf(file.name if hasattr(file, "name") else file, converter_name=converter_name)
    _reset_panel_cache()  # new document: the panel must repaint even if the HTML happens to match
    return _poll_with_image()


def on_converter_change(converter_name):
    controller.reload_with_converter(converter_name)
    _reset_panel_cache()
    return _poll_with_image()


def on_play_pause():
    if controller.state == "PLAYING":
        controller.pause()
    else:
        controller.play()
    return poll()


def on_next_sentence():
    controller.next_sentence()
    return poll()


def on_prev_sentence():
    controller.prev_sentence()
    return poll()


def on_next_page():
    controller.next_page()
    return _poll_with_image()


def on_prev_page():
    controller.prev_page()
    return _poll_with_image()


def on_audio_ended():
    controller.sentence_audio_ended()
    return poll()


def on_seek(payload: str):
    """payload is a JSON string {"page": int, "idx": int, "offset_ms": int},
    written into the hidden textbox by a click on a word/sentence span.
    Takes no outputs of its own — the Timer (already ticking every 300ms)
    picks up the resulting state on its own next tick. Giving this handler
    outputs that overlap the Timer's caused the two to race: the seek's own
    render would get silently dropped until some later, unrelated event."""
    if not payload:
        return
    try:
        data = json.loads(payload)
        controller.seek_to_word(data["page"], data["idx"], data["offset_ms"])
    except (ValueError, KeyError):
        logger.warning("ignoring malformed seek payload: %r", payload)


_logo_src = f"/gradio_api/file={os.path.join(_HERE, 'docs', 'logo.svg')}"

with gr.Blocks(title="PDF Immersive Reading") as demo:
    gr.HTML(
        f'<h1 style="display:flex;align-items:center;justify-content:center;gap:0.4em;margin:0.5em 0">'
        f'<img src="{_logo_src}" alt="" style="height:2em;width:2em">'
        f"PDF Immersive Reading</h1>"
        f"<div style='text-align:center'><strong>Engine:</strong> <code>{engine_name}</code> &nbsp;|&nbsp; <strong>Voice:</strong> <code>{voice}</code></div>"
    )
    warning_md = gr.Markdown(controller.rtf_warning or "")

    with gr.Row():
        with gr.Column(scale=3):
            upload_btn = gr.UploadButton("📄 Open PDF", file_types=[".pdf"])
            page_image = gr.Image(
                label="Page", interactive=False, show_label=False, buttons=[], elem_id="rd-page-image"
            )
            page_label = gr.Markdown("Page 0 / 0")
            with gr.Group():
                with gr.Row():
                    prev_page_btn = gr.Button("⏮️", min_width=48)
                    prev_sent_btn = gr.Button("⏪", min_width=48)
                    play_pause_btn = gr.Button("⏯️", min_width=48)
                    next_sent_btn = gr.Button("⏩", min_width=48)
                    next_page_btn = gr.Button("⏭️", min_width=48)
                with gr.Row():
                    converter_dropdown = gr.Dropdown(
                        choices=list(CONVERTERS.keys()),
                        value=controller.converter_name,
                        label="Converter",
                        show_label=True,
                        container=True,
                        scale=1,
                    )

                    speed_dropdown = gr.Dropdown(
                        choices=[(f"{s:.2f}x", s) for s in SPEED_TICKS],
                        value=1.0,
                        label="Speed",
                        scale=1,
                    )

        with gr.Column(scale=1):
            text_panel = gr.HTML()
    control_html = gr.HTML()
    advance_btn = gr.Button("advance", elem_id="rd-advance-btn", elem_classes=["rd-hidden"])
    seek_payload = gr.Textbox(elem_id="rd-seek-payload", elem_classes=["rd-hidden"])
    seek_btn = gr.Button("seek", elem_id="rd-seek-btn", elem_classes=["rd-hidden"])

    # matched to poll() / _poll_with_image() respectively — see their comment
    PANEL_OUTPUTS = [text_panel, control_html, page_label, warning_md]
    IMAGE_OUTPUTS = [page_image, *PANEL_OUTPUTS]

    upload_btn.upload(
        on_pdf_uploaded, inputs=[upload_btn, converter_dropdown], outputs=IMAGE_OUTPUTS
    )
    converter_dropdown.change(on_converter_change, inputs=[converter_dropdown], outputs=IMAGE_OUTPUTS)
    next_page_btn.click(on_next_page, outputs=IMAGE_OUTPUTS)
    prev_page_btn.click(on_prev_page, outputs=IMAGE_OUTPUTS)

    play_pause_btn.click(on_play_pause, outputs=PANEL_OUTPUTS)
    next_sent_btn.click(on_next_sentence, outputs=PANEL_OUTPUTS)
    prev_sent_btn.click(on_prev_sentence, outputs=PANEL_OUTPUTS)
    advance_btn.click(on_audio_ended, outputs=PANEL_OUTPUTS)
    seek_btn.click(on_seek, inputs=[seek_payload])

    speed_dropdown.change(fn=None, inputs=[speed_dropdown], js=_SPEED_CHANGE_JS)

    # Outputs the image too, so a PDF opened from the command line is on screen
    # at first paint (the Timer's own tick only carries PANEL_OUTPUTS).
    def _on_page_load():
        _reset_panel_cache()  # see its docstring
        return _poll_with_image()

    demo.load(_on_page_load, outputs=IMAGE_OUTPUTS)

    timer = gr.Timer(POLL_INTERVAL_S)
    timer.tick(poll, outputs=PANEL_OUTPUTS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read a PDF aloud with live highlighting.")
    parser.add_argument("pdf", nargs="?", help="PDF to open at startup instead of using the Open PDF button")
    args = parser.parse_args()
    if args.pdf:
        if not os.path.isfile(args.pdf):
            parser.error(f"no such file: {args.pdf}")
        controller.load_pdf(args.pdf, converter_name=controller.converter_name)

    # HOST/PORT env vars override config.yaml — lets `docker run -e PORT=...`
    # (or any other launcher) pick the port without editing the config file.
    host = os.environ.get("HOST", config["server"]["host"])
    port = int(os.environ.get("PORT", config["server"]["port"]))
    demo.launch(
        server_name=host,
        server_port=port,
        allowed_paths=[cache_dir_abs, os.path.join(_HERE, "docs")],
        head=_HEAD,
        favicon_path="docs/logo.svg",
    )
