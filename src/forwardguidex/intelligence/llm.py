"""Thin OpenRouter chat client."""
from __future__ import annotations

import requests

from ..config import get_settings

API_URL = "https://openrouter.ai/api/v1/chat/completions"


def chat(messages: list[dict], model: str | None = None,
         temperature: float = 0.4, max_tokens: int = 1400) -> str:
    s = get_settings()
    if not s.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")
    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {s.openrouter_api_key}",
            "HTTP-Referer": "https://github.com/a-schnu/forwardguidex",
            "X-Title": "ForwardGuidex",
        },
        json={
            "model": model or s.openrouter_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
