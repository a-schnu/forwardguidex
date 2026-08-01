"""Thin OpenRouter chat client (with retry/backoff on transient errors)."""
from __future__ import annotations

import time

import requests

from ..config import get_settings

API_URL = "https://openrouter.ai/api/v1/chat/completions"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


def chat(messages: list[dict], model: str | None = None,
         temperature: float = 0.4, max_tokens: int = 1400) -> str:
    s = get_settings()
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    payload = {
        "model": model or s.openrouter_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {s.openrouter_api_key}",
        "HTTP-Referer": "https://github.com/a-schnu/forwardguidex",
        "X-Title": "ForwardGuidex",
    }
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=180)
        if resp.status_code in _RETRY_STATUS:
            last_error = requests.HTTPError(
                f"{resp.status_code} from OpenRouter: {resp.text[:200]}")
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue
            raise last_error
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    raise last_error  # pragma: no cover
