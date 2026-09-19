"""Provider-agnostic LLM client with explicit provider selection and fallback.

Production behavior:
- Provider credentials are read only from app.config/settings.
- Demo mode is explicit via DEMO_MODE=true.
- Missing provider credentials never silently switch a production deployment
  into demo mode.
- Gemini, Groq, OpenAI and Anthropic are supported.
- LLM_PROVIDER can pin a single provider or use "auto" for a configured chain.

Telemetry separates logical LLM requests from provider API attempts.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_KNOWN_PROVIDERS = ("gemini", "groq", "openai", "anthropic")

_KEY_PREFIX = {
    "openai": "sk-",
    "anthropic": "sk-ant-",
    "groq": "gsk_",
}


def _warn_if_malformed(name: str, key: Optional[str]) -> None:
    """Log a warning for obviously malformed known key formats."""
    if not key:
        return

    prefix = _KEY_PREFIX.get(name)
    if prefix and not key.startswith(prefix):
        logger.warning(
            "%s_API_KEY is set but does not look like a valid %s key "
            "(expected it to start with '%s')",
            name.upper(),
            name,
            prefix,
        )


def _resolve_provider_order() -> List[str]:
    """Resolve the provider chain from explicit configuration and credentials.

    Explicit providers pin the chain to that provider only:
        gemini, groq, openai, anthropic, demo

    In auto mode, Gemini is preferred when configured, followed by Groq,
    OpenAI, and Anthropic. Only providers with credentials are included.
    """
    forced = settings.llm_provider

    if forced == "demo":
        return ["demo"]

    if forced in _KNOWN_PROVIDERS:
        return [forced]

    # "auto" is deterministic: prefer Gemini, then Groq, then OpenAI,
    # then Anthropic. This can be changed later without touching provider
    # implementation code.
    configured = {
        "gemini": bool(settings.gemini_api_key),
        "groq": bool(settings.groq_api_key),
        "openai": bool(settings.openai_api_key),
        "anthropic": bool(settings.anthropic_api_key),
    }

    return [name for name in ("gemini", "groq", "openai", "anthropic") if configured[name]]


class LLMError(Exception):
    """Raised when no configured LLM provider can complete a request."""


class ChatMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _GeminiClient:
    """Google Gemini client using the official google-genai SDK."""

    def complete(self, messages: List[ChatMessage]) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)

        system_parts = [
            message.content
            for message in messages
            if message.role == "system"
        ]
        user_parts = [
            message.content
            for message in messages
            if message.role != "system"
        ]

        response = client.models.generate_content(
            model=settings.gemini_model,
            contents="\n".join(user_parts),
            config=types.GenerateContentConfig(
                system_instruction="\n".join(system_parts) or None,
                temperature=0,
            ),
        )

        return getattr(response, "text", "") or ""


class _GroqClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        from groq import Groq

        client = Groq(
            api_key=settings.groq_api_key,
            base_url=settings.groq_base_url,
        )

        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            temperature=0,
        )

        return response.choices[0].message.content or ""


class _OpenAIClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        from openai import OpenAI

        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )

        payload = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]

        try:
            response = client.responses.create(
                model=settings.openai_model,
                input=payload,
            )
            return getattr(response, "output_text", "") or ""
        except Exception:
            response = client.chat.completions.create(
                model=settings.openai_model,
                messages=payload,
                temperature=0,
            )
            return response.choices[0].message.content or ""


class _AnthropicClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        import anthropic

        client = anthropic.Anthropic(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
        )

        system = "\n".join(
            message.content
            for message in messages
            if message.role == "system"
        )

        user = "\n".join(
            message.content
            for message in messages
            if message.role != "system"
        )

        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=system or None,
            messages=[{"role": "user", "content": user}],
        )

        return "".join(
            block.text
            for block in response.content
            if block.type == "text"
        )


class DemoClient:
    """Deterministic offline provider for explicit demo/test mode only."""

    def complete(self, messages: List[ChatMessage]) -> str:
        import json
        import re

        user = "\n".join(
            message.content
            for message in messages
            if message.role == "user"
        )

        system = "\n".join(
            message.content
            for message in messages
            if message.role == "system"
        )

        low_sys = system.lower()

        if "review_text" in low_sys or (
            "reviews" in low_sys and "rating" in low_sys
        ):
            lines = [
                match.group(1).strip()
                for match in re.finditer(
                    r"^Review:\s*(.+)$",
                    user,
                    re.M | re.I,
                )
            ]

            return json.dumps(
                {
                    "reviews": [
                        {
                            "review_text": text,
                            "rating": None,
                            "review_date": None,
                            "reviewer": None,
                        }
                        for text in lines
                    ]
                },
                ensure_ascii=False,
            )

        if "sentiment" in low_sys and "topics" in low_sys:
            return json.dumps(
                {
                    "sentiment": "neutral",
                    "topics": {
                        "battery": 5,
                        "comfort": 4,
                        "charging": 3,
                        "price": 3,
                        "support": 2,
                    },
                    "complaints": [
                        "Charging case is bulky and the cable is too short.",
                        "Customer support took three days to reply.",
                    ],
                    "praise": [
                        "Battery life is amazing.",
                        "Sound quality is superb, noise cancellation works great.",
                    ],
                    "feature_severities": [
                        {"feature": "charging", "severity": 3}
                    ],
                },
                ensure_ascii=False,
            )

        def grab(label: str) -> str:
            match = re.search(
                rf"^{label}:\s*(.+)$",
                user,
                re.M | re.I,
            )
            return match.group(1).strip() if match else ""

        price = grab("Price") or "0"

        try:
            price_f = round(float(price), 2)
        except ValueError:
            price_f = 0.0

        product = grab("Product")

        if not product:
            match = re.search(r"^#\s+(.+)$", user, re.M)
            product = (
                match.group(1).strip()
                if match
                else "Unknown Product"
            )

        url_match = re.search(
            r"^Page URL:\s*(.+)$",
            user,
            re.M,
        )

        url = (
            url_match.group(1).strip()
            if url_match
            else "demo://unknown"
        )

        return json.dumps(
            {
                "product_name": product,
                "brand": grab("Brand") or None,
                "price": price_f,
                "currency": grab("Currency") or "USD",
                "region": grab("Region") or "US",
                "availability": _norm_avail(
                    grab("Availability")
                ),
                "seller": grab("Seller") or None,
                "listing_title": None,
                "url": url,
            },
            ensure_ascii=False,
        )


def _norm_avail(value: str) -> str:
    value = value.strip().lower()

    return (
        value
        if value in {
            "in_stock",
            "out_of_stock",
            "preorder",
            "unknown",
        }
        else "unknown"
    )


class LLMClient:
    """LLM facade with explicit provider selection and safe fallback."""

    def __init__(self) -> None:
        self._providers: List[tuple[str, object]] = []
        self._current = 0

        self.calls = 0
        self.provider_attempts = 0
        self.fallbacks: List[tuple[str, str]] = []
        self.last_error: Optional[str] = None

        self._fallback_since: Optional[float] = None
        self.cooldown_seconds = 60

        if settings.demo_mode:
            self._providers = [("demo", DemoClient())]
            logger.warning(
                "DEMO_MODE=true — using deterministic demo LLM provider"
            )
            return

        provider_order = _resolve_provider_order()

        factories = {
            "gemini": _GeminiClient,
            "groq": _GroqClient,
            "openai": _OpenAIClient,
            "anthropic": _AnthropicClient,
        }

        keys = {
            "gemini": settings.gemini_api_key,
            "groq": settings.groq_api_key,
            "openai": settings.openai_api_key,
            "anthropic": settings.anthropic_api_key,
        }

        for name in provider_order:
            if name not in _KNOWN_PROVIDERS:
                continue

            key = keys[name]

            if not key:
                logger.warning(
                    "LLM provider '%s' selected but its API key is missing",
                    name,
                )
                continue

            _warn_if_malformed(name, key)
            self._providers.append((name, factories[name]()))

        if not self._providers:
            raise LLMError(
                "No LLM provider is configured. Set LLM_PROVIDER and its "
                "corresponding API key, or explicitly set DEMO_MODE=true "
                "for offline/demo use."
            )

        logger.info(
            "LLM provider chain resolved: %s",
            " -> ".join(name for name, _ in self._providers),
        )

    def complete(self, system: str, user: str) -> str:
        if not self._providers:
            raise LLMError("No LLM provider is configured")

        self.calls += 1

        last_error: Optional[Exception] = None

        if (
            self._current != 0
            and self._fallback_since is not None
            and (
                time.monotonic() - self._fallback_since
                > self.cooldown_seconds
            )
        ):
            self._current = 0

        previous_provider = self._providers[self._current][0]

        for offset in range(len(self._providers)):
            index = (self._current + offset) % len(self._providers)
            name, client = self._providers[index]

            self.provider_attempts += 1

            try:
                output = client.complete(
                    [
                        ChatMessage("system", system),
                        ChatMessage("user", user),
                    ]
                )

                if not output.strip():
                    raise LLMError(
                        f"provider '{name}' returned an empty response"
                    )

                if name != previous_provider:
                    self.fallbacks.append(
                        (previous_provider, name)
                    )
                    self._fallback_since = time.monotonic()

                if index == 0:
                    self._fallback_since = None

                self._current = index
                self.last_error = None

                return output

            except Exception as exc:
                last_error = exc
                self.last_error = str(exc)

                logger.warning(
                    "provider '%s' failed: %s — trying next configured provider",
                    name,
                    exc,
                )

        raise LLMError(
            f"All configured LLM providers failed: {last_error}"
        )

    @property
    def provider_name(self) -> str:
        return self._providers[self._current][0]

    def telemetry(self) -> dict:
        return {
            "provider": self.provider_name,
            "fallback_used": bool(self.fallbacks),
            "fallback_from": (
                self.fallbacks[0][0]
                if self.fallbacks
                else None
            ),
            "fallback_to": (
                self.fallbacks[0][1]
                if self.fallbacks
                else None
            ),
            "fallbacks": list(self.fallbacks),
            "calls": self.calls,
            "provider_attempts": self.provider_attempts,
            "fallbacks_count": len(self.fallbacks),
            "last_error": self.last_error,
        }

    def reset_telemetry(self) -> None:
        self.calls = 0
        self.provider_attempts = 0
        self.fallbacks = []
        self.last_error = None
        self._fallback_since = None
