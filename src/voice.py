"""
Phase 8 — Voice + Hotkey Trigger.
Phase 9 — Wake Word Detection ("hey jarvis"), layered on top.

Local speech-to-text (faster-whisper) combined with a global hotkey
listener (pynput), always-on wake-word detection (openWakeWord, Phase 9),
and a system tray icon (pystray), replacing the terminal input() loop
entirely when VOICE_ENABLED=true.

Two ways to start a recording:
  1. Hotkey (Phase 8) — press VOICE_HOTKEY to start, press it again to
     stop (toggle mode, manual on both ends).
  2. Wake word (Phase 9, opt-in via WAKE_WORD_ENABLED) — say "hey jarvis"
     any time the agent is idle. A short confirmation beep plays, then
     it records automatically and stops itself once you pause speaking
     (silence-based auto-stop) — you never touch anything.

Either way, once a recording stops: audio is transcribed locally via
faster-whisper (no cloud STT call), and the transcribed text is handed
to the on_task callback main.py supplies, which runs it through the
exact same graph.invoke() pipeline the text REPL uses — voice is just a
different way to produce a task string; nothing downstream changes.

Mic access is exclusive — only one audio stream runs at a time. See
WAKE_WORD_ENABLED's config comment for the full exclusivity/ordering
rules between wake-word listening, hotkey recording, and task execution.

Requires: faster-whisper, sounddevice, pynput, pystray, numpy, and
(only if WAKE_WORD_ENABLED) openwakeword — NOT in the base dependency
list (see pyproject.toml's 'voice' extras group). Only imported when
VOICE_ENABLED=true.

IMPORTANT — testing limitations: this module was written and reviewed
without access to a microphone, a display for the tray icon, or a real
hotkey-capable OS session (built and verified in a sandboxed Linux
container). Every third-party API used here (faster-whisper,
sounddevice's InputStream, pynput's GlobalHotKeys, openWakeWord's Model)
was verified against current documentation and, where possible, exercised
with synthetic data and fakes standing in for real hardware — but this
phase needs real verification on your machine more than code review
alone can provide. In particular, WAKE_WORD_SILENCE_RMS_THRESHOLD and
WAKE_WORD_SILENCE_TIMEOUT_S (Phase 9's auto-stop) are mic- and
room-dependent and were never tunable against real audio anywhere in
this build — treat their defaults as a starting guess.
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
    WHISPER_LANGUAGE,
    VOICE_SAMPLE_RATE,
    VOICE_MAX_RECORDING_SECONDS,
    WAKE_WORD_ENABLED,
    WAKE_WORD_MODEL,
    WAKE_WORD_THRESHOLD,
    WAKE_WORD_SILENCE_RMS_THRESHOLD,
    WAKE_WORD_SILENCE_TIMEOUT_S,
    WAKE_WORD_MIN_SPEECH_S,
    WAKE_WORD_MAX_RECORDING_SECONDS,
    WAKE_WORD_MIC_GAIN,
    VOICE_DEVICE_INDEX,
    WAKE_WORD_SPEAKER_THRESHOLD,
    WAKE_WORD_COOLDOWN_S,
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
    C library isn't present on the system, or pystray needs a GTK
    backend on Linux. Reporting "not installed" for either case would
    send someone down the wrong troubleshooting path (re-running pip
    install, which "succeeds" and changes nothing) — so each failure's
    actual message is captured and shown instead of a generic one.
    """
    checks = [
        ("faster_whisper", "faster-whisper"),
        ("sounddevice", "sounddevice"),
        ("pynput", "pynput"),
        ("pystray", "pystray"),
        ("numpy", "numpy"),
    ]
    if WAKE_WORD_ENABLED:
        checks.append(("openwakeword", "openwakeword"))

    problems = []
    for module_name, pip_name in checks:
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


def _play_wake_chime() -> None:
    """
    Best-effort audible confirmation that the wake word was heard —
    stands in for the "asks you what to do" behavior a real voice
    assistant would give, without building a full text-to-speech
    pipeline (a much bigger separate feature). winsound is part of
    Python's standard library on Windows — this codebase is already
    Windows-only (pywin32, uiautomation, pycaw), so this adds no new
    dependency. Never let a failure here interrupt the actual recording
    flow — it's a nice-to-have cue, not load-bearing.
    """
    try:
        import winsound
        winsound.Beep(880, 150)
    except Exception as e:
        log.debug("Wake chime failed (non-fatal): %s", e)


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
    Owns the audio recording buffer, the Whisper model, the wake-word
    detector, the hotkey listener, and the tray icon.

    on_task(text) is called with the transcribed task string every time
    a recording completes with non-empty text. Called synchronously and
    BLOCKS until it returns (main.py's _run_task() blocks on
    graph.invoke()) — this is relied on deliberately, see
    _transcribe_and_dispatch's docstring for why.
    """

    def __init__(
        self,
        on_task: Callable[[str], None],
        on_status_change: Optional[Callable[[str], None]] = None,
    ):
        check_voice_dependencies()

        self._on_task = on_task
        # Phase 10: optional hook so a consumer without a tray icon (the
        # desktop app) can still observe idle/recording/processing
        # status changes — see _set_status(). None is a no-op, so this
        # is fully backward compatible with Phase 8/9's tray-only usage.
        self._on_status_change = on_status_change

        # -- shared recording state (hotkey- and wakeword-triggered both
        #    go through the same _start_recording/_stop_recording_and_process) --
        self._recording = False
        self._active_trigger: Optional[str] = None  # "hotkey" | "wakeword"
        self._task_running = False
        self._audio_chunks: list = []
        self._record_lock = threading.Lock()
        self._stream = None
        self._record_start_time: Optional[float] = None

        # -- Phase 9: silence-based auto-stop bookkeeping (wakeword only) --
        self._heard_speech_since: Optional[float] = None
        self._last_loud_time: Optional[float] = None

        # -- Phase 9: wake-word listening stream (separate from the
        #    above recording stream — only one of the two is ever
        #    active at a time, see module docstring) --
        self._wakeword_stream = None
        self._wakeword_detector = None

        # Phase 10: the hotkey listener is now stored on self (was a
        # local variable inside run() before) so start_background_
        # listening()/stop_background_listening() can manage it
        # independently of whether a tray icon (run()) is involved at all.
        self._hotkey_listener = None

        self._icon = None
        # Lazy-loaded on first recording, not at construction — avoids
        # paying Whisper's model-load cost (can be several seconds,
        # longer on first run if it needs to download) before the tray
        # icon even appears, so startup feels immediate.
        self._model = None
        self._cached_meter = None
        self._last_suppression_log_time = 0.0
        # Cooldown: timestamp (monotonic) before which wake word
        # detections are ignored — prevents instant false re-triggers
        # from the model's internal rolling buffer retaining residual
        # scores from the wake phrase that was just spoken.
        self._wakeword_resume_time: float = 0.0

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
        # during silence (e.g. the moment between triggering a recording
        # and actually starting to speak) — voice activity detection
        # trims that out before transcription runs on it.
        segments, _info = model.transcribe(
            audio,
            language=WHISPER_LANGUAGE or None,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        return " ".join(seg.text.strip() for seg in segments).strip()

    # -- Recording audio (shared by both trigger paths) ------------------

    def _audio_callback(self, indata, frames, time_info, status):
        if status:
            now = time.monotonic()
            if now - getattr(self, '_last_rec_overflow_log', 0) >= 5.0:
                log.warning("Audio input status: %s", status)
                self._last_rec_overflow_log = now

        with self._record_lock:
            if not self._recording:
                return
            self._audio_chunks.append(indata.copy())
            is_wakeword_triggered = self._active_trigger == "wakeword"

        if is_wakeword_triggered:
            # Real-time energy check for Phase 9's silence-based
            # auto-stop — deliberately a simple RMS threshold, not
            # Whisper's own (much more accurate) vad_filter, which only
            # runs after the fact on the complete recording and so can't
            # be used to decide WHEN to stop in the first place.
            import numpy as np
            rms = float(np.sqrt(np.mean(indata.astype("float64") ** 2)))
            if rms >= WAKE_WORD_SILENCE_RMS_THRESHOLD:
                now = time.monotonic()
                with self._record_lock:
                    self._last_loud_time = now
                    if self._heard_speech_since is None:
                        self._heard_speech_since = now

    def _start_recording(self, trigger: str = "hotkey"):
        import sounddevice as sd

        with self._record_lock:
            if self._recording or self._task_running:
                return
            self._audio_chunks = []
            self._recording = True
            self._active_trigger = trigger
            self._record_start_time = time.monotonic()
            self._heard_speech_since = None
            self._last_loud_time = time.monotonic()

        try:
            self._stream = sd.InputStream(
                device=VOICE_DEVICE_INDEX,
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
                self._active_trigger = None
            self._set_status(_Status.IDLE)
            if trigger == "wakeword":
                self._start_wakeword_listening()
            return

        self._set_status(_Status.RECORDING)

        if trigger == "wakeword":
            log.info(
                "Recording started (wake word) — will stop automatically "
                "once you pause, or after %ds regardless",
                WAKE_WORD_MAX_RECORDING_SECONDS,
            )
            threading.Thread(target=self._silence_watchdog, daemon=True).start()
            max_seconds = WAKE_WORD_MAX_RECORDING_SECONDS
        else:
            log.info("Recording started — press %s again to stop", VOICE_HOTKEY)
            max_seconds = VOICE_MAX_RECORDING_SECONDS

        threading.Thread(
            target=self._auto_stop_watchdog, args=(max_seconds,), daemon=True
        ).start()

    def _auto_stop_watchdog(self, max_seconds: float):
        """Safety cap for BOTH trigger paths — if the stop condition
        (hotkey press, or silence for the wakeword path) is somehow
        missed, don't record forever."""
        time.sleep(max_seconds)
        with self._record_lock:
            still_recording = self._recording
        if still_recording:
            log.warning(
                "Hit the %ds recording cap without a stop condition — "
                "stopping automatically", max_seconds,
            )
            self._stop_recording_and_process()

    def _silence_watchdog(self):
        """
        Phase 9 only: polls for silence following speech and auto-stops
        a wakeword-triggered recording. Hotkey-triggered recordings are
        never touched by this — they stop only on an explicit second
        hotkey press (or the shared max-duration cap above).
        """
        poll_interval_s = 0.1
        while True:
            time.sleep(poll_interval_s)
            with self._record_lock:
                if not self._recording or self._active_trigger != "wakeword":
                    return  # recording ended some other way, or wasn't ours to watch
                heard_speech_since = self._heard_speech_since
                last_loud = self._last_loud_time

            now = time.monotonic()
            if heard_speech_since is None:
                continue  # hasn't heard any speech above the RMS threshold yet
            if now - heard_speech_since < WAKE_WORD_MIN_SPEECH_S:
                continue  # require a minimum amount of speech before allowing auto-stop
            if last_loud is not None and now - last_loud >= WAKE_WORD_SILENCE_TIMEOUT_S:
                log.info("Silence detected after speech — stopping recording")
                self._stop_recording_and_process()
                return

    def _stop_recording_and_process(self):
        with self._record_lock:
            if not self._recording:
                return
            self._recording = False
            chunks = self._audio_chunks
            self._audio_chunks = []
            trigger = self._active_trigger
            self._active_trigger = None

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
            if trigger == "wakeword":
                self._start_wakeword_listening()
            return

        self._set_status(_Status.PROCESSING)

        # Off the calling thread (hotkey callback or silence-watchdog
        # thread) so a slow CPU transcription doesn't block either from
        # being responsive.
        threading.Thread(
            target=self._transcribe_and_dispatch, args=(chunks, trigger), daemon=True
        ).start()

    def _transcribe_and_dispatch(self, chunks, trigger: Optional[str]):
        import pythoncom
        pythoncom.CoInitialize()
        try:
            self._transcribe_and_dispatch_inner(chunks, trigger)
        finally:
            pythoncom.CoUninitialize()

    def _transcribe_and_dispatch_inner(self, chunks, trigger: Optional[str]):
        """
        trigger is only used here to know whether to resume wake-word
        listening afterward (only relevant if it was wakeword-triggered
        or if wake-word listening should generally resume once idle —
        see the resume calls below, which fire regardless of trigger so
        a hotkey-triggered task still lets wake-word listening resume
        once it's done).

        IMPORTANT: self._on_task(text) is called synchronously here and
        blocks until it returns — main.py's _run_task() blocks on
        graph.invoke(). This is relied on deliberately: wake-word
        listening and the hotkey's ability to START a new recording are
        both gated on self._task_running, which stays True for exactly
        the duration of this blocking call. That's what prevents a wake
        word overheard mid-task (or an accidental hotkey press) from
        starting a second, overlapping graph.invoke() — see
        WAKE_WORD_ENABLED's config comment for the full reasoning.
        """
        import numpy as np

        try:
            audio = np.concatenate(chunks, axis=0).flatten()
            text = self._transcribe(audio)
        except Exception as e:
            log.error("Transcription failed: %s", e, exc_info=True)
            self._set_status(_Status.IDLE)
            self._start_wakeword_listening()
            return

        self._set_status(_Status.IDLE)

        if not text:
            log.warning("Transcription produced no text — nothing to run")
            self._start_wakeword_listening()
            return

        log.info("Transcribed: %s", text)

        with self._record_lock:
            self._task_running = True
        try:
            self._on_task(text)
        except Exception as e:
            log.error("Task callback raised: %s", e, exc_info=True)
        finally:
            with self._record_lock:
                self._task_running = False
            # Resume wake-word listening only now that the task has
            # fully finished — never while recording, transcribing, or
            # running a task. See this method's docstring.
            self._start_wakeword_listening()

    # -- Hotkey toggle -----------------------------------------------------

    def _on_hotkey(self):
        with self._record_lock:
            currently_recording = self._recording
            task_running = self._task_running

        if task_running and not currently_recording:
            log.info("A task is already running — ignoring hotkey until it finishes")
            return

        if currently_recording:
            self._stop_recording_and_process()
        else:
            self._start_recording(trigger="hotkey")

    # -- Wake word (Phase 9) ------------------------------------------------

    def _get_speaker_meter(self):
        if self._cached_meter is not None:
            return self._cached_meter
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            from ctypes import POINTER, cast
            from comtypes import CLSCTX_ALL

            speakers = AudioUtilities.GetSpeakers()
            if speakers and speakers._dev:
                iface = speakers._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
                self._cached_meter = cast(iface, POINTER(IAudioMeterInformation))
                return self._cached_meter
        except Exception as e:
            log.debug("Failed to initialize speaker meter: %s", e)
        self._cached_meter = None
        return None

    def _get_wakeword_detector(self):
        if self._wakeword_detector is None:
            from src.wakeword import WakeWordDetector
            self._wakeword_detector = WakeWordDetector()
        return self._wakeword_detector

    def _wakeword_audio_callback(self, indata, frames, time_info, status):
        if status:
            now = time.monotonic()
            if now - getattr(self, '_last_ww_overflow_log', 0) >= 5.0:
                log.warning("Wake word audio input status: %s", status)
                self._last_ww_overflow_log = now

        with self._record_lock:
            if self._recording or self._task_running:
                return  # busy — ignore, don't trigger a second overlapping task

        # Cooldown: ignore detections for a short window after resuming
        # wake word listening — the model's internal rolling context
        # buffer retains residual high scores from the wake phrase just
        # spoken, causing instant false re-triggers without this.
        if time.monotonic() < self._wakeword_resume_time:
            return

        # Check system audio playback — instead of fully suppressing
        # wake word detection (which permanently blocks it during music
        # playback), we raise the required score so the user can still
        # trigger by speaking clearly over the audio.
        speaker_active = False
        if WAKE_WORD_SPEAKER_THRESHOLD > 0.0:
            try:
                meter = self._get_speaker_meter()
                if meter is not None:
                    peak = meter.GetPeakValue()
                    if peak >= WAKE_WORD_SPEAKER_THRESHOLD:
                        speaker_active = True
                        now = time.monotonic()
                        if now - self._last_suppression_log_time >= 10.0:
                            log.info(
                                "System audio playing (peak=%.3f) — wake word threshold boosted to reduce false triggers",
                                peak
                            )
                            self._last_suppression_log_time = now
            except Exception as e:
                log.debug("COM error reading speaker meter: %s", e)
                self._cached_meter = None

        try:
            import numpy as np
            detector = self._get_wakeword_detector()
            chunk = indata[:, 0]
            # Apply software gain — many laptop mic arrays produce very
            # low-level signals that the model can't score without
            # amplification. Clip to [-1, 1] after gain to avoid
            # distortion artifacts that could confuse the model.
            if WAKE_WORD_MIC_GAIN != 1.0:
                chunk = np.clip(chunk * WAKE_WORD_MIC_GAIN, -1.0, 1.0)
            scores = detector.predict(chunk)
        except Exception as e:
            log.error("Wake word prediction failed: %s", e)
            return

        # When system audio is playing, require a much stronger detection
        # to avoid self-triggering from the system's own output, while
        # still allowing deliberate wake word attempts from the user.
        effective_threshold = WAKE_WORD_THRESHOLD
        if speaker_active:
            effective_threshold = min(WAKE_WORD_THRESHOLD * 1.7, 0.90)

        for name, score in scores.items():
            if score >= effective_threshold:
                log.info("Wake word '%s' detected (score=%.2f)", name, score)
                # Stop wake-word listening immediately: only one audio
                # stream at a time, and this also prevents re-triggering
                # repeatedly on the same utterance while we transition
                # into recording.
                self._stop_wakeword_listening()
                _play_wake_chime()
                self._notify("Yes? Listening...")
                self._start_recording(trigger="wakeword")
                return  # only act on the first hit in this chunk

    def _start_wakeword_listening(self):
        if not WAKE_WORD_ENABLED:
            return

        with self._record_lock:
            if self._recording or self._task_running:
                return  # don't resume while busy — the caller that
                         # finishes being busy is responsible for
                         # calling this again afterward

        if self._wakeword_stream is not None:
            return  # already listening

        import sounddevice as sd
        from src.wakeword import WAKE_WORD_CHUNK_SAMPLES

        # Reset the wake word detector's internal rolling buffer so
        # residual scores from the previous detection don't immediately
        # cause a false re-trigger.
        try:
            detector = self._get_wakeword_detector()
            if hasattr(detector, 'reset'):
                detector.reset()
        except Exception as e:
            log.debug("Could not reset wake word detector: %s", e)

        # Set cooldown: ignore all detections for WAKE_WORD_COOLDOWN_S
        # seconds from now, giving the mic time to "clear" any residual
        # audio from the previous wake word utterance.
        self._wakeword_resume_time = time.monotonic() + WAKE_WORD_COOLDOWN_S

        try:
            self._get_wakeword_detector()  # load eagerly here, not on
                                            # first chunk, so the delay
                                            # happens once at startup
                                            # rather than stalling the
                                            # first real "hey jarvis"
            self._wakeword_stream = sd.InputStream(
                device=VOICE_DEVICE_INDEX,
                samplerate=VOICE_SAMPLE_RATE,
                channels=1,
                dtype="float32",
                blocksize=WAKE_WORD_CHUNK_SAMPLES,
                callback=self._wakeword_audio_callback,
            )
            self._wakeword_stream.start()
            log.info(
                "Wake word listening active ('%s', threshold=%.2f)",
                WAKE_WORD_MODEL, WAKE_WORD_THRESHOLD,
            )
        except Exception as e:
            log.error(
                "Could not start wake word listening: %s — wake word "
                "disabled for this session, the hotkey still works", e,
            )
            self._wakeword_stream = None

    def _stop_wakeword_listening(self):
        if self._wakeword_stream is not None:
            try:
                self._wakeword_stream.stop()
                self._wakeword_stream.close()
            except Exception:
                pass
            self._wakeword_stream = None

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

        if self._on_status_change is not None:
            try:
                self._on_status_change(status)
            except Exception as e:
                log.debug("Status change callback failed (non-fatal): %s", e)

    def _notify(self, message: str, title: str = "Computer-Use Agent"):
        """
        Best-effort OS-level notification via the tray icon (a real
        Windows toast/balloon on pystray's Windows backend) — added so
        there's a clear, visible signal for "the agent has started and
        is listening" and "I heard the wake word," beyond just a
        terminal log line someone running this in the background won't
        see. Never let a failure here — unsupported platform, a backend
        quirk — break the actual voice flow: the terminal log and (for
        wake-word detection specifically) the audible beep are the
        load-bearing confirmations; this is a nice-to-have on top.
        """
        if self._icon is None:
            return
        try:
            if getattr(self._icon, "HAS_NOTIFICATION", False):
                self._icon.notify(message, title)
            else:
                log.debug("Tray notifications not supported on this platform/backend")
        except Exception as e:
            log.debug("Tray notification failed (non-fatal): %s", e)

    def _on_quit(self, icon, item):
        log.info("Voice mode: quitting")
        icon.stop()

    def start_background_listening(self) -> None:
        """
        Non-blocking: starts the hotkey listener and (if
        WAKE_WORD_ENABLED) wake-word listening — no tray icon, no
        window, no other main-thread-owning loop. Safe to call at most
        once per VoiceController instance.

        Two callers:
          - run() (Phase 8/9's tray-icon voice mode) calls this, then
            additionally runs the tray icon's own blocking loop.
          - Phase 10's desktop app (src/desktop_app.py) calls this once
            at startup, then runs pywebview's blocking loop instead of
            a tray icon — no tray icon is created in that mode at all,
            the app window IS the visible presence.

        Both pynput's GlobalHotKeys and sounddevice's InputStream run
        their own background threads internally — neither needs to own
        the calling thread, which is what makes this decomposition
        possible. Only a tray icon's or a GUI window's own event loop
        needs the main thread.
        """
        from pynput import keyboard

        self._hotkey_listener = keyboard.GlobalHotKeys({VOICE_HOTKEY: self._on_hotkey})
        self._hotkey_listener.start()

        if WAKE_WORD_ENABLED:
            self._start_wakeword_listening()

        wake_word_note = ', or say "hey jarvis"' if WAKE_WORD_ENABLED else ""
        log.info(
            "Voice listening active — press %s to start/stop recording a task%s",
            VOICE_HOTKEY, wake_word_note,
        )

    def stop_background_listening(self) -> None:
        """Counterpart to start_background_listening() — stops the hotkey
        listener, wake-word listening, and any in-progress recording stream."""
        if self._hotkey_listener is not None:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None

        self._stop_wakeword_listening()

        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _on_tray_ready(self, icon):
        """
        pystray's run(setup=...) callback — fires once in a separate
        thread after the event loop has actually started, meaning the
        icon is genuinely visible in the system tray by this point (not
        just constructed). Providing a custom setup function means we're
        responsible for setting icon.visible ourselves — pystray's
        default setup (used when no setup= is given) does this
        automatically, a custom one must replicate it.

        Only handles tray-specific setup now — the actual hotkey/
        wake-word listening is started separately by run() calling
        start_background_listening() before the icon's event loop even
        begins, so it's already active by the time this fires.
        """
        icon.visible = True

        if WAKE_WORD_ENABLED:
            ready_message = f'Ready — say "hey jarvis" or press {VOICE_HOTKEY} to start a task.'
        else:
            ready_message = f"Ready — press {VOICE_HOTKEY} to start a task."
        self._notify(ready_message)

    def run(self):
        """
        Blocking — owns the calling thread. Call this from main.py's main
        thread (pystray's run loop expects to own its calling thread on
        some platforms; this codebase is Windows-specific already via
        pywin32/uiautomation, so keep this on the main thread regardless).
        """
        import pystray

        self.start_background_listening()

        self._icon = pystray.Icon(
            "computer_use_agent",
            _make_icon_image(_Status.IDLE),
            f"Computer-Use Agent — idle (press {VOICE_HOTKEY} to talk)",
            menu=pystray.Menu(
                pystray.MenuItem("Quit", self._on_quit),
            ),
        )

        try:
            self._icon.run(setup=self._on_tray_ready)  # blocks until Quit
        finally:
            self.stop_background_listening()


def run_voice_loop(on_task: Callable[[str], None]) -> None:
    """
    Entry point called from main.py when VOICE_ENABLED=true. Blocks until
    the tray icon's Quit item is used.
    """
    controller = VoiceController(on_task)
    controller.run()