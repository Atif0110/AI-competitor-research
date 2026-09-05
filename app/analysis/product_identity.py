"""Product identity resolution (engineering-review #7: conservative tiered matching).

Tiers (documented; this build implements tier 3 deterministically):
    1. SKU / model number          -> not implemented (schema-ready)
    2. GTIN / UPC / EAN            -> not implemented (schema-ready)
    3. normalized name             -> THIS resolves: brand+model tokens
    4. fuzzy / LLM confirmation    -> roadmap (ambiguous cases only)

Only generic filler tokens are stripped; variant markers (ANC, gen 2/3, v2,
Wireless) are KEPT so materially different products do not collapse. Names
that reduce to fewer than 2 significant tokens never collapse either.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import List

# Generic filler only. ANC / gen / v2 / wireless / bluetooth are variant
# markers and deliberately kept — collapsing them created false matches.
NOISE_TOKENS = {"with", "the", "and", "series", "edition", "new", "plus", "model"}

# Conservative synonym map applied before token comparison.
ALIASES = {
    "headset": "headphones",
    "earphone": "headphones",
}

# Collapse only when we still have >= 2 significant tokens after filtering.
MIN_TOKENS_FOR_COLLAPSE = 2

_TOKEN_RE = re.compile(r"[a-z0-9]+")


@dataclass
class ProductIdentity:
    canonical_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)


def normalize_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return re.sub(r"\s+", " ", s).strip() or "unknown"


def canonical_tokens(name: str) -> List[str]:
    out: List[str] = []
    for t in _TOKEN_RE.findall(name.lower()):
        t = ALIASES.get(t, t)
        if t in NOISE_TOKENS:
            continue
        if t not in out:
            out.append(t)
    return out


def resolve(name: str) -> ProductIdentity:
    if not name or not name.strip():
        name = "unknown"
    tokens = canonical_tokens(name)
    if len(tokens) >= MIN_TOKENS_FOR_COLLAPSE:
        canon = " ".join(tokens)
    else:
        # Too few significant tokens -> never collapse; keep the full name.
        canon = normalize_name(name)
    cid = hashlib.sha256(canon.encode()).hexdigest()[:16]
    return ProductIdentity(canonical_id=cid, canonical_name=canon)
