"""LLM providers behind one function. Order comes from AI_PROVIDERS; a failing provider falls through."""
import re
import threading
import time

import requests
from django.conf import settings


class AIError(Exception):
    pass


def _groq(messages: list[dict], json_mode: bool, temperature: float, model: str | None = None, can_wait: bool = True) -> str:
    if not settings.GROQ_API_KEY:
        raise AIError("GROQ_API_KEY not set")
    model = model or settings.GROQ_MODEL
    body = {"model": model, "messages": messages, "temperature": temperature}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"  # far fewer hidden tokens: faster, and stays under the free tokens-per-minute cap
    def call():
        return requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROQ_API_KEY}"}, json=body, timeout=60,
        )
    r = call()
    for _ in range(2):
        if r.status_code == 429:  # per-minute token cap: waiting for the window to clear usually works
            m = re.search(r"try again in ([\d.]+)s", r.text)
            wait = float(m.group(1)) + 1 if m else float(r.headers.get("retry-after", 8))
            if can_wait and wait <= 40:  # only wait when there is no other model left to try
                time.sleep(wait)
                r = call()
                continue
        if r.status_code == 400 and "json_validate_failed" in r.text:  # model returned an empty/invalid JSON body: just ask again
            r = call()
            continue
        break
    if r.status_code != 200:
        raise AIError(f"groq {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"]["content"]


def _gemini(messages: list[dict], json_mode: bool, temperature: float) -> str:
    if not settings.GEMINI_API_KEY:
        raise AIError("GEMINI_API_KEY not set")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages if m["role"] != "system"
    ]
    body = {"contents": contents, "generationConfig": {"temperature": temperature}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": settings.GEMINI_API_KEY}, json=body, timeout=60,
    )
    if r.status_code != 200:
        raise AIError(f"gemini {r.status_code}: {r.text[:200]}")
    try:
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise AIError("gemini returned no text")


PROVIDERS = {"groq": _groq, "gemini": _gemini}


_LOCK = threading.Lock()  # one model call at a time per process: concurrent page loads would otherwise trip rate limits


def complete(messages: list[dict], json_mode: bool = False, temperature: float = 0.2) -> tuple[str, str]:
    """Returns (text, provider_name). Raises AIError if every provider fails."""
    with _LOCK:
        return _complete(messages, json_mode, temperature)


def _chain() -> list[tuple[str, callable]]:
    """Ordered attempts: every Groq model (each has its own rate limit), then the other providers."""
    out = []
    for name in settings.AI_PROVIDERS:
        if name == "groq":
            models = [settings.GROQ_MODEL] + [m for m in settings.GROQ_FALLBACK_MODELS if m != settings.GROQ_MODEL]
            for i, m in enumerate(models):
                last = i == len(models) - 1  # only the last Groq model waits out a short rate limit
                out.append((f"groq:{m}", lambda msgs, jm, t, m=m, last=last: _groq(msgs, jm, t, m, can_wait=last)))
        elif name in PROVIDERS:
            out.append((name, PROVIDERS[name]))
    return out


def _complete(messages: list[dict], json_mode: bool, temperature: float) -> tuple[str, str]:
    errors = []
    for label, fn in _chain():
        try:
            return fn(messages, json_mode, temperature), label.split(":")[0]
        except (AIError, requests.RequestException) as e:
            errors.append(f"{label}: {e}")
    raise AIError("All AI providers failed. " + " | ".join(errors) if errors else "No AI provider configured")
