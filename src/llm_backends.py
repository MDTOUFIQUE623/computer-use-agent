"""
Phase 7 — pluggable LLM backends for Brain's planning calls.

Brain used to hold a raw `google.genai.Client()` and call
`self._client.models.generate_content(...)` directly, hardcoding Gemini
into the planning/decompose/replan logic. This module gives Brain a
single small interface instead (LLMClient.generate_content), with Gemini
and Ollama as the two built-in implementations and a registry so a third
backend can be added later without touching Brain, or this file's
existing classes, at all.

Adding a new backend (e.g. OpenAI, LM Studio, a different local runtime):
  1. Write a class implementing generate_content(...) -> str (see
     LLMClient below for the exact contract) and a `model_name` attribute.
  2. Call register_llm_backend("your_name", YourClass) once, anywhere
     that runs before Brain() is constructed (e.g. top of main.py).
  3. Set PLANNER_BACKEND=your_name.
No changes to brain.py, config.py, or the rest of this file are needed
for that — this is the actual point of the registry, not just a nicer
if/elif.

NOTE: this only covers Brain's planning/decompose/replan calls. Vision
fallback (src/tools/vision.py) and memory embeddings (src/memory.py)
still use Gemini directly — swapping those is a separate, larger effort
(most local models don't have comparable vision or embedding support
to what those two currently rely on), out of scope for Phase 7.
"""
import logging
import time
from typing import Callable, Optional, Protocol

from src.config import (
    PLANNER_MODEL,
    OLLAMA_MODEL,
    OLLAMA_BASE_URL,
    OLLAMA_REQUEST_TIMEOUT_S,
)

log = logging.getLogger(__name__)


class LLMClient(Protocol):
    """
    The entire interface Brain needs from an LLM backend. Any class
    implementing this shape can be registered and selected via
    PLANNER_BACKEND — Brain itself never checks which backend it has.
    """

    model_name: str

    def generate_content(
        self,
        *,
        system_instruction: Optional[str],
        prompt: str,
        max_output_tokens: int,
        json_mode: bool = False,
    ) -> str:
        """
        Return the raw text response.

        json_mode=True is a hint that the caller expects (and will
        json.loads) a JSON object back — implementations should honor it
        if the backend can constrain output (Gemini: response_mime_type;
        Ollama: the format parameter). It's a hint, not a guarantee:
        Brain's own parsing (markdown-stripping, try/except around
        json.loads) stays defensive regardless of backend, since even
        Gemini isn't given a strict schema today.

        Raise on unrecoverable failure — Brain's call sites already catch
        broadly and return None, matching pre-Phase-7 behavior where a
        raw Gemini exception meant "no plan this time."
        """
        ...


# ---------------------------------------------------------------------------
# Retry-with-backoff
# ---------------------------------------------------------------------------
#
# Moved here from brain.py in Phase 7 (it started as a Gemini-only fix
# after Phase 6 testing surfaced a transient 503 under concurrent load —
# see GeminiLLMClient below). Retry logic now lives with the backend
# whose errors it's actually interpreting, since "transient" means
# something different per backend: Gemini's "503 UNAVAILABLE" string vs.
# Ollama's requests.ConnectionError/Timeout are different failure shapes
# entirely, and only the backend that produced an error knows how to
# tell transient apart from real.

def call_with_retry(
    fn: Callable[[], str],
    *,
    is_transient: Callable[[Exception], bool],
    max_attempts: int = 3,
    base_delay_s: float = 1.5,
) -> str:
    """
    Call fn() (a zero-arg callable wrapping one LLM request). On a
    transient error (per is_transient), wait base_delay_s * attempt and
    retry, up to max_attempts total tries. On a non-transient error, or
    after the last attempt, re-raises — callers already catch broadly
    and return None, this only adds retries, it doesn't change what
    "give up" looks like.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if attempt < max_attempts and is_transient(e):
                delay = base_delay_s * attempt
                log.warning(
                    "Transient LLM backend error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, max_attempts, delay, e,
                )
                time.sleep(delay)
                continue
            raise last_exc
    raise last_exc  # pragma: no cover — loop always returns or raises above


# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------

_GEMINI_TRANSIENT_MARKERS = (
    "503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "high demand",
)


def _is_transient_gemini_error(e: Exception) -> bool:
    text = str(e)
    return any(marker in text for marker in _GEMINI_TRANSIENT_MARKERS)


class GeminiLLMClient:
    """Wraps google.genai.Client() behind the LLMClient interface."""

    model_name = PLANNER_MODEL

    def __init__(self):
        from google import genai
        self._client = genai.Client()

    def generate_content(
        self,
        *,
        system_instruction: Optional[str],
        prompt: str,
        max_output_tokens: int,
        json_mode: bool = False,
    ) -> str:
        from google.genai import types

        def _call() -> str:
            config_kwargs = {"max_output_tokens": max_output_tokens}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction
            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"

            response = self._client.models.generate_content(
                model=PLANNER_MODEL,
                contents=[prompt],
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return response.text or ""

        return call_with_retry(_call, is_transient=_is_transient_gemini_error)


# ---------------------------------------------------------------------------
# Ollama backend
# ---------------------------------------------------------------------------

def _is_transient_ollama_error(e: Exception) -> bool:
    import requests
    # A refused/timed-out connection usually means Ollama is still
    # starting up, briefly overloaded, or the model is still loading into
    # memory on first use — worth a retry. An HTTPError (model doesn't
    # exist, malformed request, etc.) is a real problem retrying won't fix.
    return isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout))


class OllamaLLMClient:
    """
    Talks to a locally-running `ollama serve` over its HTTP API
    (default http://localhost:11434 — see OLLAMA_BASE_URL) instead of a
    cloud API. Model is whatever OLLAMA_MODEL is set to — not hardcoded
    to any specific model, since the whole point is you can point this
    at qwen3:8b today and something else you pull next month without
    touching code, just the env var.

    Accepts that local mode will generally be lower-reliability than
    Gemini for structured JSON planning output, especially on smaller
    models — mitigated two ways: (1) requests Ollama's `format: "json"`
    mode when json_mode=True, which constrains the model's raw token
    output to valid JSON at the Ollama level, not just via a "please
    respond in JSON" prompt instruction; (2) Brain's existing response
    parsing (markdown-stripping + defensive try/except) is unchanged and
    backend-agnostic, so a local model's rougher output still gets every
    chance the Gemini path already got.
    """

    def __init__(self):
        self.model_name = OLLAMA_MODEL
        self._base_url = OLLAMA_BASE_URL.rstrip("/")
        self._check_available()

    def _check_available(self) -> None:
        """
        Fail fast, with a clear and actionable error, if Ollama isn't
        reachable at all — at Brain() construction time, rather than
        deep inside a task's retry loop where the real problem ("ollama
        serve" isn't running) would be much harder to spot.
        """
        import requests
        try:
            resp = requests.get(f"{self._base_url}/api/tags", timeout=5)
            resp.raise_for_status()
        except Exception as e:
            raise RuntimeError(
                f"Could not reach Ollama at {self._base_url} — "
                f"is `ollama serve` running? ({e})"
            ) from e

        models = [m.get("name") or m.get("model", "") for m in resp.json().get("models", [])]
        if self.model_name not in models:
            log.warning(
                "OLLAMA_MODEL='%s' isn't in `ollama list` (found: %s). Ollama "
                "may try to pull/run it anyway on first use, which can be slow "
                "or fail outright if the name's wrong — run `ollama pull %s` "
                "first to avoid a surprise mid-task.",
                self.model_name, models, self.model_name,
            )

    def generate_content(
        self,
        *,
        system_instruction: Optional[str],
        prompt: str,
        max_output_tokens: int,
        json_mode: bool = False,
    ) -> str:
        import requests

        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": max_output_tokens},
        }
        if json_mode:
            payload["format"] = "json"

        def _call() -> str:
            resp = requests.post(
                f"{self._base_url}/api/chat",
                json=payload,
                timeout=OLLAMA_REQUEST_TIMEOUT_S,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "") or ""

        return call_with_retry(_call, is_transient=_is_transient_ollama_error)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

LLM_BACKENDS: dict[str, Callable[[], LLMClient]] = {
    "gemini": GeminiLLMClient,
    "ollama": OllamaLLMClient,
}


def register_llm_backend(name: str, factory: Callable[[], LLMClient]) -> None:
    """
    Register a new backend (or replace a built-in one) at runtime — see
    this module's docstring for the 3-step process to add one. `factory`
    is anything callable with no arguments that returns an LLMClient,
    almost always just the class itself (its constructor).
    """
    LLM_BACKENDS[name] = factory
    log.info("Registered LLM backend: %s", name)


def get_llm_client(backend_name: str) -> LLMClient:
    """
    Build and return the LLMClient for `backend_name` (from
    config.PLANNER_BACKEND). Raises ValueError with the list of
    currently-registered backends if the name isn't recognized — this
    is deliberately loud rather than silently falling back to Gemini,
    since a typo'd PLANNER_BACKEND should be caught immediately, not
    discovered later as "why is this using Gemini when I set Ollama?"
    """
    factory = LLM_BACKENDS.get(backend_name)
    if factory is None:
        available = ", ".join(sorted(LLM_BACKENDS.keys()))
        raise ValueError(
            f"Unknown PLANNER_BACKEND='{backend_name}'. "
            f"Available: {available}. Register a new one via "
            f"src.llm_backends.register_llm_backend()."
        )
    return factory()