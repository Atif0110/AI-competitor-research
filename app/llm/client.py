"""Provider-agnostic chat client with a real cross-provider fallback chain
and circuit breaker.

Provider selection (v5 — auto-switch on whichever key is present):
    - ANTHROPIC_API_KEY only  -> Claude is primary
    - OPENAI_API_KEY only     -> GPT is primary
    - both set                -> LLM_PROVIDER decides primary ("openai" or
                                  "anthropic"); defaults to Claude if unset.
                                  Whichever of the two is NOT primary becomes
                                  the automatic fallback.
    - GROQ_API_KEY            -> always appended to the END of the chain as an
                                  optional fast/cheap extra fallback, never
                                  forced ahead of GPT/Claude.
    - LLM_PROVIDER can also be forced to a single provider name ("groq",
      "openai", "anthropic", "demo") to pin the chain to exactly that one.
    - no keys at all          -> demo provider (unchanged).

Telemetry separates API attempts from logical requests:
    llm_calls         = logical requests (1 per complete())
    provider_attempts = actual provider API calls (Claude fail + GPT ok = 2)
    fallbacks         = number of provider switches

A lightweight circuit breaker re-probes the primary after `cooldown_seconds`.
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_KNOWN_PROVIDERS = ("groq", "openai", "anthropic")

# Loose sanity checks — not full validation, just enough to catch a pasted
# placeholder or the wrong key in the wrong slot before it silently falls
# through to demo mode and leaves you wondering why.
_KEY_PREFIX = {"openai": "sk-", "anthropic": "sk-ant-", "groq": "gsk_"}


def _warn_if_malformed(name: str, key: Optional[str]) -> None:
    if not key:
        return
    prefix = _KEY_PREFIX.get(name)
    if prefix and not key.startswith(prefix):
        logger.warning(
            "%s_API_KEY is set but doesn't look like a valid %s key "
            "(expected it to start with '%s') — check for a copy-paste mistake",
            name.upper(), name, prefix)


def _resolve_provider_order() -> List[str]:
    """Build provider priority from which keys are actually set, with GPT and
    Claude treated as the two primaries and Groq as a trailing extra.

    LLM_PROVIDER behaves as:
        "demo"               -> pin to demo, exclusively
        "groq"                -> pin to groq, exclusively (explicit single-provider mode)
        "openai"/"anthropic" -> PREFERENCE for which of the two leads the chain
                                 when both keys are present; does not exclude
                                 the other — it still follows as fallback.
        "auto" / unset        -> Claude leads if both are present, Groq trails.
    """
    forced = settings.llm_provider
    if forced in ("demo", "groq"):
        return [forced]

    have_openai = bool(settings.openai_api_key)
    have_anthropic = bool(settings.anthropic_api_key)
    have_groq = bool(settings.groq_api_key)

    order: List[str] = []
    if have_openai and have_anthropic:
        # both primaries configured — LLM_PROVIDER (if openai/anthropic)
        # picks which leads; default to Claude as primary otherwise
        preferred = forced if forced in ("openai", "anthropic") else "anthropic"
        order.append(preferred)
        order.append("openai" if preferred == "anthropic" else "anthropic")
    elif have_anthropic:
        order.append("anthropic")
    elif have_openai:
        order.append("openai")

    if have_groq:
        order.append("groq")

    return order


class LLMError(Exception):
    pass


class ChatMessage:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


class _GroqClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        from groq import Groq

        client = Groq(api_key=settings.groq_api_key)
        resp = client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=0,
        )
        return resp.choices[0].message.content or ""


class _OpenAIClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            temperature=0,
        )
        return resp.choices[0].message.content or ""


class _AnthropicClient:
    def complete(self, messages: List[ChatMessage]) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        system = "\n".join(m.content for m in messages if m.role == "system")
        user = "\n".join(m.content for m in messages if m.role != "system")
        resp = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=2048,
            system=system or None,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in resp.content if block.type == "text")


class DemoClient:
    """Deterministic offline provider."""

    def complete(self, messages: List[ChatMessage]) -> str:
        import json
        import re

        user = "\n".join(m.content for m in messages if m.role == "user")
        system = "\n".join(m.content for m in messages if m.role == "system")
        low_sys = system.lower()

        if "review_text" in low_sys or ("reviews" in low_sys and "rating" in low_sys):
            lines = [m.group(1).strip() for m in re.finditer(r"^Review:\s*(.+)$", user, re.M | re.I)]
            return json.dumps({"reviews": [
                {"review_text": t, "rating": None, "review_date": None, "reviewer": None}
                for t in lines]}, ensure_ascii=False)

        if "sentiment" in low_sys and "topics" in low_sys:
            return json.dumps({
                "sentiment": "neutral",
                "topics": {"battery": 5, "comfort": 4, "charging": 3, "price": 3, "support": 2},
                "complaints": ["Charging case is bulky and the cable is too short.",
                               "Customer support took three days to reply."],
                "praise": ["Battery life is amazing.",
                           "Sound quality is superb, noise cancellation works great."],
                "feature_severities": [{"feature": "charging", "severity": 3}],
            })

        def grab(label: str) -> str:
            m = re.search(rf"^{label}:\s*(.+)$", user, re.M | re.I)
            return m.group(1).strip() if m else ""

        price = grab("Price") or "0"
        try:
            price_f = round(float(price), 2)
        except ValueError:
            price_f = 0.0
        product = grab("Product")
        if not product:
            m = re.search(r"^#\s+(.+)$", user, re.M)
            product = m.group(1).strip() if m else "Unknown Product"
        um = re.search(r"^Page URL:\s*(.+)$", user, re.M)
        url = um.group(1).strip() if um else "demo://unknown"
        return json.dumps({
            "product_name": product,
            "brand": grab("Brand") or None,
            "price": price_f,
            "currency": grab("Currency") or "USD",
            "region": grab("Region") or "US",
            "availability": _norm_avail(grab("Availability")),
            "seller": grab("Seller") or None,
            "listing_title": None,
            "url": url,
        }, ensure_ascii=False)


def _norm_avail(value: str) -> str:
    v = value.strip().lower()
    return v if v in {"in_stock", "out_of_stock", "preorder", "unknown"} else "unknown"


class LLMClient:
    """Facade with real fallback chain + per-request/attempt telemetry."""

    def __init__(self) -> None:
        self._providers: List[tuple[str, object]] = []
        self._current = 0
        self.calls = 0            # logical requests
        self.provider_attempts = 0  # actual provider API calls
        self.fallbacks: List[tuple[str, str]] = []
        self.last_error: Optional[str] = None
        self._fallback_since: Optional[float] = None
        self.cooldown_seconds = 60

        if settings.llm_provider == "demo" or settings.demo_mode:
            self._providers = [("demo", DemoClient())]
            return

        want = _resolve_provider_order()
        _factory = {"groq": _GroqClient, "openai": _OpenAIClient, "anthropic": _AnthropicClient}
        _key = {"groq": settings.groq_api_key, "openai": settings.openai_api_key,
                "anthropic": settings.anthropic_api_key}
        for name in want:
            if name not in _KNOWN_PROVIDERS:
                continue
            key = _key[name]
            if not key:
                logger.warning("LLM_PROVIDER=%s requested but no matching API key is set", name)
                continue
            _warn_if_malformed(name, key)
            self._providers.append((name, _factory[name]()))

        if not self._providers:
            logger.warning("no LLM keys configured — falling back to demo provider")
            self._providers = [("demo", DemoClient())]
        else:
            logger.info("LLM provider chain resolved: %s", " -> ".join(n for n, _ in self._providers))

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        last_error: Optional[Exception] = None
        # circuit breaker: re-probe primary after cooldown
        if (self._current != 0 and self._fallback_since is not None
                and (time.monotonic() - self._fallback_since) > self.cooldown_seconds):
            self._current = 0
        prev = self._providers[self._current][0]
        for i in range(len(self._providers)):
            idx = (self._current + i) % len(self._providers)
            name, client = self._providers[idx]
            self.provider_attempts += 1
            try:
                out = client.complete([ChatMessage("system", system), ChatMessage("user", user)])
                if name != prev:
                    self.fallbacks.append((prev, name))
                    self._fallback_since = time.monotonic()
                if idx == 0:
                    self._fallback_since = None
                self._current = idx
                return out
            except Exception as e:
                last_error = e
                self.last_error = str(e)
                logger.warning("provider '%s' failed: %s — trying next", name, e)
        raise LLMError(f"all LLM providers failed: {last_error}")

    @property
    def provider_name(self) -> str:
        return self._providers[self._current][0]

    def telemetry(self) -> dict:
        return {
            "provider": self.provider_name,
            "fallback_used": bool(self.fallbacks),
            "fallback_from": self.fallbacks[0][0] if self.fallbacks else None,
            "fallback_to": self.fallbacks[0][1] if self.fallbacks else None,
            "fallbacks": list(self.fallbacks),
            "calls": self.calls,
            "provider_attempts": self.provider_attempts,
            "fallbacks_count": len(self.fallbacks),
        }

    def reset_telemetry(self) -> None:
        self.calls = 0
        self.provider_attempts = 0
        self.fallbacks = []
        self.last_error = None
        self._fallback_since = None
