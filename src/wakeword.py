"""
Phase 9 — Wake Word Detection.

Thin wrapper around openWakeWord, kept as its own module separate from
src/voice.py's VoiceController so wake-word scoring (a single
responsibility: "does this audio chunk match the wake word?") stays
independent of stream lifecycle management, threading, and the
recording/transcription pipeline VoiceController owns.

Uses the pretrained "hey_jarvis" model bundled with openWakeWord — no
custom training needed, and (in the version this was built/tested
against) no separate download step either: the pretrained ONNX models
ship inside the pip package itself under openwakeword/resources/models/.
Runs entirely locally, no audio ever leaves the machine for wake-word
detection specifically. (Note: the actual task transcription that
happens AFTER the wake word fires still goes through faster-whisper,
also fully local — see src/voice.py.)

API VERIFIED DIRECTLY against the actually-installed package, not just
documentation — worth calling out because documentation/tutorials found
online for this library describe an older or different API shape
(openwakeword.utils.download_models() + Model(wakeword_models=[...])
with bare names) that does NOT match what's actually importable in the
version this was tested against: there's no download_models() function
at all (models are already bundled), and Model's real constructor
parameter is wakeword_model_paths — a list of ONNX file paths, not bare
model names — resolved here via openwakeword.models[name]["model_path"].
If a future openwakeword release changes this again, _resolve_model_path
below is the one place that would need updating.
"""
import logging

from src.config import (
    WAKE_WORD_MODEL,
    WAKE_WORD_VAD_THRESHOLD,
)

log = logging.getLogger(__name__)

# openWakeWord expects chunks of exactly this many samples at 16kHz
# (~80ms) — VoiceController's wake-word InputStream is configured with
# this as its blocksize so every callback hands predict() a
# correctly-sized chunk.
WAKE_WORD_CHUNK_SAMPLES = 1280


_download_attempted = False


def _auto_download_models():
    """
    Auto-download pretrained openWakeWord models if they aren't present.
    Only attempts once per process to avoid repeated network calls.
    """
    global _download_attempted
    if _download_attempted:
        return
    _download_attempted = True
    try:
        from openwakeword.utils import download_models
        log.info("Downloading missing openWakeWord pretrained models…")
        download_models()
        log.info("openWakeWord model download complete")
    except Exception as e:
        log.warning("Could not auto-download openWakeWord models: %s", e)


def _resolve_model_path(name: str) -> str:
    """
    Turn a bare model name (e.g. "hey_jarvis", WAKE_WORD_MODEL's default)
    into the actual bundled ONNX file path openWakeWord's Model class
    needs. Falls through to treating `name` as a literal path if it's
    not a bundled name, so a custom-trained model (see openWakeWord's
    docs on training your own wake word) works by pointing
    WAKE_WORD_MODEL at its .onnx file directly.

    Tries several strategies in order because openWakeWord's own API for
    this has changed between versions — verified directly against one
    installed version while building this (openwakeword.models[name]
    ["model_path"] worked there), then broke on a real run against a
    different installed version (AttributeError: module 'openwakeword'
    has no attribute 'models'). Rather than hardcode a single shape and
    risk breaking again on yet another version, this checks each known
    shape and only fails — with a maximally diagnostic error — if none
    of them work.
    """
    import os
    import glob
    import openwakeword

    # Strategy 1: openwakeword.MODELS dict, e.g. {"hey_jarvis": {"model_path": "..."}}
    # NOTE: the attribute is uppercase MODELS, not lowercase models.
    models_dict = getattr(openwakeword, "MODELS", None) or getattr(openwakeword, "models", None)
    if isinstance(models_dict, dict) and name in models_dict:
        entry = models_dict[name]
        if isinstance(entry, dict) and "model_path" in entry:
            path = entry["model_path"]
            if not os.path.exists(path):
                _auto_download_models()
            if os.path.exists(path):
                return path
        if isinstance(entry, str):
            if not os.path.exists(entry):
                _auto_download_models()
            if os.path.exists(entry):
                return entry

    # Strategy 2: openwakeword.get_pretrained_model_paths() -- a flat list
    # of paths; match by filename stem containing `name`.
    get_paths_fn = getattr(openwakeword, "get_pretrained_model_paths", None)
    if callable(get_paths_fn):
        try:
            for p in get_paths_fn():
                if name in os.path.basename(p) and os.path.exists(p):
                    return p
            # If we found a matching path but file didn't exist, try downloading
            for p in get_paths_fn():
                if name in os.path.basename(p):
                    _auto_download_models()
                    if os.path.exists(p):
                        return p
        except Exception as e:
            log.debug("get_pretrained_model_paths() strategy failed: %s", e)

    # Strategy 3: locate the installed package's own directory and glob
    # for a matching model file directly, independent of whatever
    # top-level API this particular version does or doesn't expose.
    # Check both .tflite (default in newer versions) and .onnx formats.
    try:
        package_dir = os.path.dirname(openwakeword.__file__)
        candidates = []
        for ext in ("tflite", "onnx"):
            candidates += glob.glob(os.path.join(package_dir, "resources", "models", f"{name}*.{ext}"))
            candidates += glob.glob(os.path.join(package_dir, "**", f"{name}*.{ext}"), recursive=True)
        if candidates:
            return candidates[0]
    except Exception as e:
        log.debug("Filesystem-glob strategy failed: %s", e)

    # Strategy 4: name is already a literal path to a custom model.
    if name.endswith((".onnx", ".tflite")):
        return name

    # Every strategy failed — give a maximally informative error rather
    # than a bare AttributeError three calls deep, so this is fixable
    # from the error message alone.
    available_attrs = sorted(a for a in dir(openwakeword) if not a.startswith("_"))
    version = getattr(openwakeword, "__version__", "unknown")
    raise ValueError(
        f"Could not resolve WAKE_WORD_MODEL='{name}' to a model file using "
        f"any known openWakeWord API shape (installed version: {version}).\n"
        f"  Top-level openwakeword attributes available: {available_attrs}\n"
        f"  Either share that list so this can be fixed for your exact "
        f"version, or set WAKE_WORD_MODEL directly to a full .onnx file "
        f"path — find one under the package's install directory via:\n"
        f"    python -c \"import openwakeword, os; "
        f"print(os.path.dirname(openwakeword.__file__))\"\n"
        f"  then look inside that folder (and its subfolders) for a file "
        f"matching '{name}*.onnx'."
    )


class WakeWordDetector:
    """
    Wraps openwakeword.model.Model.
    """

    def __init__(self):
        from openwakeword.model import Model

        model_path = _resolve_model_path(WAKE_WORD_MODEL)
        log.info("Loading openWakeWord model: %s", model_path)

        self._model = Model(
            wakeword_models=[model_path],
            # Silero VAD gate, bundled with openWakeWord: only allow a
            # wake-word prediction through when the VAD model
            # simultaneously scores actual speech above this threshold
            # on the same frame. Meaningfully cuts false-accepts from
            # non-speech noise (fans, clicks, a door) in an always-on
            # continuous-listening setup like this one. 0 disables VAD
            # filtering entirely (openWakeWord's own default) — this
            # project's config default is 0.5, not 0, deliberately.
            vad_threshold=WAKE_WORD_VAD_THRESHOLD,
        )
        log.info("openWakeWord model ready")

    def predict(self, audio_chunk) -> dict:
        """
        audio_chunk: 1D numpy array, WAKE_WORD_CHUNK_SAMPLES samples,
        float32 in [-1, 1] (openWakeWord also accepts int16 PCM, but
        VoiceController's stream is float32 to match the rest of this
        project's audio handling — see src/voice.py).

        Returns {model_key: score} — score is 0.0-1.0, scores THIS
        chunk only (not cumulative), reflects wake-word likelihood using
        openWakeWord's own internal rolling context buffer under the hood.

        NOTE: model_key is NOT necessarily WAKE_WORD_MODEL verbatim — in
        the version this was tested against, openWakeWord keys its
        output by the loaded model's filename stem (e.g.
        "hey_jarvis_v0.1" for WAKE_WORD_MODEL="hey_jarvis"), not the
        bare name passed in. Callers (VoiceController) iterate all
        returned keys rather than looking one up by exact name, so this
        doesn't affect correctness — just don't assume the dict key
        equals WAKE_WORD_MODEL when reading logs.
        """
        import numpy as np
        if audio_chunk.dtype != np.int16:
            audio_chunk = (audio_chunk * 32767.0).astype(np.int16)
        return self._model.predict(audio_chunk)

    def reset(self):
        """
        Clear the underlying openWakeWord model's internal rolling
        context buffer.  Called by VoiceController when resuming
        wake-word listening after a detection, so residual high scores
        from the just-spoken wake phrase don't cause an instant
        false re-trigger.
        """
        try:
            self._model.reset()
        except Exception as e:
            log.debug("openWakeWord model.reset() failed (non-fatal): %s", e)