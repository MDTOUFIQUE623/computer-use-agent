"""
Phase 8 — Voice + Hotkey Trigger.

Local speech-to-text (faster-whisper) combined with a global hotkey
listener (pynput) and a system tray icon (pystray), replacing the
terminal input() loop entirely when VOICE_ENABLED=true. Orthogonal to
every other phase — this module and main.py's entry point are the only
things that changed.

Flow:
  1. Press VOICE_HOTKEY to start recording (toggle mode — not
     push-to-talk; see VOICE_HOTKEY's config comment for why).
  2. Speak your task.
  3. Press the same hotkey again to stop.
  4. Audio is transcribed locally via faster-whisper — no network call,
     no cloud STT service.
  5. The transcribed text is handed to the on_task callback main.py
     supplies, which runs it through the exact same graph.invoke()
     pipeline the text REPL uses — voice is just a different way to
     produce a task string; nothing downstream changes.

Requires: faster-whisper, sounddevice, pynput, pystray, numpy — NOT in
the base dependency list (see pyproject.toml's 'voice' extras group),
since they pull in extra system-level requirements (PortAudio for
sounddevice, a Whisper model download) that most users of this agent
via the terminal won't need. Only imported when VOICE_ENABLED=true.

IMPORTANT — testing limitations: this module was written and reviewed
without access to a microphone, a display for the tray icon, or a real
hotkey-capable OS session (built and verified in a sandboxed Linux
container). The faster-whisper API shape, sounddevice's InputStream
pattern, and pynput's GlobalHotKeys format were all verified against
current documentation, and everything here is written as defensively
as I can manage without hands-on testing — but this phase needs real
verification on your machine more than any prior phase did. Expect to
iterate on VOICE_HOTKEY (in case of a conflict with another app),
WHISPER_MODEL_SIZE (speed vs. accuracy), and possibly the toggle
behavior itself once you've actually used it.
"""
import logging
import threading
import time
from typing import Callable, Optional

from src.config import (
    VOICE_HOTKEY,
    WHISPER_MODEL_SIZE,
    WHISPER_DEVICE,
    WHISPER_COMPUTE_TYPE,
    VOICE_SAMPLE_RATE,
    VOICE_MAX_RECORDING_SECONDS,
)

log = logging.getLogger(__name__)


class VoiceDependencyError(RuntimeError):
    """Raised when VOICE_ENABLED=true but the voice extras aren't installed."""


def check_voice_dependencies() -> None:
    """
    Called before doing anything else voice-related, so a missing or
    broken dependency fails immediately with clear, specific information
    rather than as a confusing traceback several calls deep once someone's
    already mid-task.

    Deliberately catches Exception, not just ImportError: a package can
    be pip-installed but still fail to import for a system-level reason
    — e.g. sounddevice imports fine as a Python package but raises
    OSError('PortAudio library not found') if the underlying PortAudio
    C library isn't present on the system. Reporting "not installed" for
    that case would send someone down the wrong troubleshooting path
    (re-running pip install, which "succeeds" and changes nothing) — so
    each failure's actual message is captured and shown instead of a
    generic one.
    """
    problems = []
    for module_name, pip_name in [
        ("faster_whisper", "faster-whisper"),
        ("sounddevice", "sounddevice"),
        ("pynput", "pynput"),
        ("pystray", "pystray"),
        ("numpy", "numpy"),
    ]:
        try:
            __import__(module_name)
        except Exception as e:
            problems.append((pip_name, str(e)))

    if problems:
        lines = [
            "VOICE_ENABLED=true but the voice dependencies aren't ready:",
            "",
        ]
        pip_installable = []
        for pip_name, error in problems:
            lines.append(f"  - {pip_name}: {error}")
            if "no module named" in error.lower():
                pip_installable.append(pip_name)

        lines.append("")
        if pip_installable:
            lines.append(f"  Missing packages — install with:")
            lines.append(f"    pip install {' '.join(pip_installable)}")
            lines.append(f"    (or `pip install -e .[voice]` — see pyproject.toml)")
        non_pip = [p for p, _ in problems if p not in pip_installable]
        if non_pip:
            lines.append(
                f"  {', '.join(non_pip)} imported but failed at a system level "
                f"(see the error above) — that usually means a missing OS-level "
                f"library (e.g. sounddevice needs PortAudio installed on the "
                f"system, not just pip-installed) rather than a Python package "
                f"problem. Check that package's install docs for your OS."
            )
        lines.append("")
        lines.append("  Or set VOICE_ENABLED=false in your .env to use the normal text input loop instead.")

        raise VoiceDependencyError("\n".join(lines))


class _Status:
    IDLE = "idle"
    RECORDING = "recording"
    PROCESSING = "processing"


def _make_icon_image(status: str):
    """Draw a simple colored circle for the tray icon — gray=idle,
    red=recording, yellow=processing. Uses Pillow, already a dependency
    of this project (see pyproject.toml) for the pptx/screenshot paths."""
    from PIL import Image, ImageDraw

    color = {
        _Status.IDLE: (120, 120, 120),
        _Status.RECORDING: (220, 50, 50),
        _Status.PROCESSING: (230, 180, 40),
    }[status]

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = 8
    draw.ellipse([margin, margin, size - margin, size - margin], fill=color)
    return image


class VoiceController:
    """
    Owns the audio recording buffer, the Whisper model, the hotkey
    listener, and the tray icon. Toggle mode only.

    on_task(text) is called with the transcribed task string every time
    a recording completes with non-empty text.
    """

    def __init__(self, on_task: Callable[[str], None]):
        check_voice_dependencies()

        self._on_task = on_task

        self._recording = False
        self._audio_chunks: list = []
        self._record_lock = threading.Lock()
        self._stream = None
        self._record_start_time: Optional[float] = None

        self._icon = None
        # Lazy-loaded on first recording, not at construction — avoids
        # paying Whisper's model-load cost (can be several seconds,
        # longer on first run if it needs to download) before the tray
        # icon even appears, so startup feels immediate.
        self._model = None

    # -- Whisper -------------------------------------------------------

    def _get_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            log.info(
                "Loading faster-whisper model '%s' (device=%s, compute_type=%s) — "
                "first run may take a while if the model needs to download",
                WHISPER_MODEL_SIZE, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE,
            )
            self._model = WhisperModel(
                WHISPER_MODEL_SIZE,
                device=WHISPER_DEVICE,
                compute_type=WHISPER_COMPUTE_TYPE,
            )
            log.info("Whisper model loaded")
        return self._model

    def _transcribe(self, audio) -> str:
        model = self._get_model()
        # vad_filter=True: Whisper is known to hallucinate phantom text
        # during silence (e.g. the moment between pressing the hotkey
        # and actually starting to speak) — voice activity detection
        # trims that out before transcription runs on it.
        segments, _info = model.transcribe(
            audio,
            language=None,  # auto-detect; hardcode "en" here if you want
                             # to shave a little latency and always speak
                             # the same language
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    # -- Audio -----------------------------------------------------------

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            log.warning("Audio input status: %s", status)
        with self._record_lock:
            if self._recording:
                self._audio_chunks.append(indata.copy())

    def _start_recording(self):
        import sounddevice as sd

        with self._record_lock:
            if self._recording:
                return
            self._audio_chunks = []
            self._recording = True
            self._record_start_time = time.monotonic()

        try:
            self._stream = sd.InputStream(
                samplerate=VOICE_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            # No microphone, permission denied, device busy, etc. — fail
            # back to idle cleanly instead of leaving _recording=True
            # with no actual stream running.
            log.error("Could not start audio recording: %s", e)
            with self._record_lock:
                self._recording = False
            self._set_status(_Status.IDLE)
            return

        self._set_status(_Status.RECORDING)
        log.info("Recording started — press %s again to stop", VOICE_HOTKEY)

        # Safety cap so a missed stop-hotkey doesn't record forever.
        threading.Thread(target=self._auto_stop_watchdog, daemon=True).start()

    def _auto_stop_watchdog(self):
        time.sleep(VOICE_MAX_RECORDING_SECONDS)
        with self._record_lock:
            still_recording = self._recording
        if still_recording:
            log.warning(
                "Hit VOICE_MAX_RECORDING_SECONDS=%ds without a stop — "
                "stopping automatically", VOICE_MAX_RECORDING_SECONDS,
            )
            self._stop_recording_and_process()

    def _stop_recording_and_process(self):
        with self._record_lock:
            if not self._recording:
                return
            self._recording = False
            chunks = self._audio_chunks
            self._audio_chunks = []

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("Error closing audio stream: %s", e)
            self._stream = None

        duration = time.monotonic() - (self._record_start_time or time.monotonic())
        log.info("Recording stopped (%.1fs captured)", duration)

        if not chunks:
            log.warning("No audio captured — nothing to transcribe")
            self._set_status(_Status.IDLE)
            return

        self._set_status(_Status.PROCESSING)

        # Off the hotkey-callback thread so a slow CPU transcription
        # doesn't block the listener from noticing the next hotkey press
        # (e.g. to start a fresh recording while this one's still
        # processing — see the module docstring's testing-limitations
        # note: this is one of the interaction edges I couldn't verify
        # hands-on and would want confirmed on real hardware).
        threading.Thread(
            target=self._transcribe_and_dispatch, args=(chunks,), daemon=True
        ).start()

    def _transcribe_and_dispatch(self, chunks):
        import numpy as np

        try:
            audio = np.concatenate(chunks, axis=0).flatten()
            text = self._transcribe(audio)
        except Exception as e:
            log.error("Transcription failed: %s", e, exc_info=True)
            self._set_status(_Status.IDLE)
            return

        self._set_status(_Status.IDLE)

        if not text:
            log.warning("Transcription produced no text — nothing to run")
            return

        log.info("Transcribed: %s", text)
        try:
            self._on_task(text)
        except Exception as e:
            log.error("Task callback raised: %s", e, exc_info=True)

    # -- Hotkey toggle -----------------------------------------------------

    def _on_hotkey(self):
        with self._record_lock:
            currently_recording = self._recording
        if currently_recording:
            self._stop_recording_and_process()
        else:
            self._start_recording()

    # -- Tray icon --------------------------------------------------------

    def _set_status(self, status: str):
        if self._icon is not None:
            try:
                self._icon.icon = _make_icon_image(status)
                self._icon.title = f"Computer-Use Agent — {status}"
            except Exception as e:
                # Tray icon updates are cosmetic — never let a failure
                # here take down actual recording/transcription.
                log.debug("Tray icon update failed (non-fatal): %s", e)

    def _on_quit(self, icon, item):
        log.info("Voice mode: quitting")
        icon.stop()

    def run(self):
        """
        Blocking — owns the calling thread. Call this from main.py's main
        thread (pystray's run loop expects to own its calling thread on
        some platforms; this codebase is Windows-specific already via
        pywin32/uiautomation, so keep this on the main thread regardless).
        """
        import pystray
        from pynput import keyboard

        listener = keyboard.GlobalHotKeys({VOICE_HOTKEY: self._on_hotkey})
        listener.start()

        self._icon = pystray.Icon(
            "computer_use_agent",
            _make_icon_image(_Status.IDLE),
            f"Computer-Use Agent — idle (press {VOICE_HOTKEY} to talk)",
            menu=pystray.Menu(
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

        log.info(
            "Voice mode active — press %s to start/stop recording a task, "
            "or use the tray icon's Quit to exit",
            VOICE_HOTKEY,
        )
        try:
            self._icon.run()  # blocks until Quit
        finally:
            listener.stop()
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass


def run_voice_loop(on_task: Callable[[str], None]) -> None:
    """
    Entry point called from main.py when VOICE_ENABLED=true. Blocks until
    the tray icon's Quit item is used.
    """
    controller = VoiceController(on_task)
    controller.run()