"""Small deterministic focus taxonomy shared by discovery and validation."""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse


_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "audio": (
        "earbud", "earbuds", "earphone", "earphones", "headphone", "headphones",
        "headset", "headsets", "bud", "buds", "airdopes", "airpods", "airpod", "tws", "wireless buds",
    ),
    "phone": (
        "smartphone", "smartphones", "mobile phone", "mobile phones", "phone", "phones",
        "iphone", "iphones", "galaxy", "pixel phone", "pixel phones",
    ),
    "wearable": (
        "smartwatch", "smartwatches", "watch", "watches", "fitness band", "fitness bands",
        "fitness tracker", "fitness trackers", "colorfit",
    ),
    "computer": (
        "laptop", "laptops", "notebook", "notebooks", "macbook", "macbooks",
        "chromebook", "chromebooks", "thinkpad",
    ),
    "television": (
        "television", "televisions", "smart tv", "smart tvs", "tv", "tvs",
    ),
}

_CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "audio": ("earbuds", "earphones", "headphones", "buds", "tws", "airdopes", "airpods"),
    "phone": ("smartphones", "mobile phones", "iphone", "galaxy", "pixel phones"),
    "wearable": ("smartwatches", "fitness bands", "fitness trackers", "colorfit"),
    "computer": ("laptops", "notebooks", "macbooks", "chromebooks", "thinkpad"),
    "television": ("televisions", "smart tvs"),
}

_GENERIC_PRODUCT_WORDS = {
    "the", "and", "with", "for", "series", "edition", "new", "pro", "plus", "max",
    "mini", "lite", "wireless", "smart", "product", "products", "model", "sku", "id",
}
_BROAD_CATEGORY_WORDS = {
    "earbud", "earbuds", "earphone", "earphones", "headphone", "headphones", "headset", "headsets",
    "smartphone", "smartphones", "phone", "phones", "watch", "watches", "smartwatch", "smartwatches",
    "laptop", "laptops", "notebook", "notebooks", "television", "televisions", "tv", "tvs",
    "tracker", "trackers", "band", "bands",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _contains_phrase(tokens: list[str], phrase_tokens: list[str]) -> bool:
    if not phrase_tokens or len(phrase_tokens) > len(tokens):
        return False
    return any(tokens[index:index + len(phrase_tokens)] == phrase_tokens for index in range(len(tokens) - len(phrase_tokens) + 1))


def _focus_token_present(token: str, text_tokens: set[str]) -> bool:
    if token in text_tokens:
        return True
    # Model families are commonly written as Galaxy S25 while a focus is
    # entered as "Galaxy S series".
    return len(token) == 1 and token.isalpha() and any(
        candidate.startswith(token) and candidate[1:].isdigit()
        for candidate in text_tokens
    )


def focus_categories(focus_products: Iterable[str]) -> set[str]:
    combined = " ".join(str(value) for value in focus_products).lower()
    tokens = _tokens(combined)
    categories: set[str] = set()
    for category, aliases in _CATEGORY_ALIASES.items():
        for alias in aliases:
            alias_tokens = _tokens(alias)
            if _contains_phrase(tokens, alias_tokens):
                categories.add(category)
                break
    return categories


def category_aliases(focus_products: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        alias
        for category in sorted(focus_categories(focus_products))
        for alias in _CATEGORY_ALIASES[category]
    )


def discovery_focus_terms(focus_products: Iterable[str], *, competitor: bool = False) -> tuple[str, ...]:
    """Use exact focus on the primary company and category equivalents for rivals."""
    focus = tuple(str(value).strip() for value in focus_products if str(value).strip())
    categories = focus_categories(focus)
    if competitor and categories:
        return tuple(term for category in sorted(categories) for term in _CATEGORY_TERMS[category])
    return focus


def focus_matches(text: str, focus_products: Iterable[str], *, allow_category_fallback: bool = True) -> bool:
    """Check whether a product label/source URL matches a focus or its category."""
    focus = tuple(str(value).strip() for value in focus_products if str(value).strip())
    if not focus:
        return True

    normalized_text = " ".join(_tokens(text))
    text_tokens = set(_tokens(text))
    if not text_tokens:
        return False

    for phrase in focus:
        phrase_tokens = [token for token in _tokens(phrase) if token not in _GENERIC_PRODUCT_WORDS]
        if phrase_tokens and all(_focus_token_present(token, text_tokens) for token in phrase_tokens):
            return True

    if not allow_category_fallback:
        return False

    categories = focus_categories(focus)
    if categories and not any(set(_tokens(phrase)) & _BROAD_CATEGORY_WORDS for phrase in focus):
        return False
    for category in categories:
        for alias in _CATEGORY_ALIASES[category]:
            alias_tokens = _tokens(alias)
            if alias in normalized_text or (len(alias_tokens) == 1 and alias_tokens[0] in text_tokens):
                return True
    # For unclassified software/service phrases, preserve useful partial path
    # matching (e.g. "team workspace" -> /product/team-hubs).
    if not categories and allow_category_fallback:
        return any(
            token in text_tokens
            for phrase in focus
            for token in _tokens(phrase)
            if token not in _GENERIC_PRODUCT_WORDS and len(token) >= 3
        )
    return False


def focus_matches_offer(
    product_name: str,
    listing_title: str | None,
    url: str,
    focus_products: Iterable[str],
    *,
    allow_category_fallback: bool = True,
) -> bool:
    label = " ".join(part for part in (product_name, listing_title or "", url) if part)
    return focus_matches(label, focus_products, allow_category_fallback=allow_category_fallback)


def product_identity_matches_source(product_name: str, url: str, content: str) -> bool:
    """Require product identity in the URL or near the page's primary heading."""
    product_tokens = [
        token for token in _tokens(product_name)
        if len(token) >= 3 and token not in _GENERIC_PRODUCT_WORDS
    ]
    if not product_tokens:
        return False

    path_segments = [segment for segment in _tokens(urlparse(url).path) if segment]
    path_tokens = set(path_segments)
    distinctive = [token for token in product_tokens if token not in {
        "boat", "noise", "samsung", "apple", "sony", "jbl", "oneplus", "realme",
        "earbuds", "earbud", "earphones", "headphones", "headphone", "watch", "phone",
        "power", "bank", "speaker", "speakers", "charger", "charging", "cable",
    }]
    identity_tokens = set(distinctive or product_tokens)
    for index, segment in enumerate(path_segments):
        if segment in {"product", "products", "item", "items"} and index + 1 < len(path_segments):
            slug_tokens = {
                token for token in path_segments[index + 1:]
                if len(token) >= 3 and any(char.isalpha() for char in token)
                and token not in _GENERIC_PRODUCT_WORDS
            }
            # A descriptive commerce slug is authoritative. Do not accept the
            # name of a related/recommended product elsewhere on that page.
            if slug_tokens:
                return bool(
                    slug_tokens & identity_tokens
                    or any(
                        slug.startswith(identity) and slug[len(identity):].isdigit()
                        for slug in slug_tokens
                        for identity in identity_tokens
                        if len(identity) >= 4
                    )
                )
            break

    if identity_tokens & path_tokens:
        return True

    # Some product URLs use opaque identifiers. In that case, require the
    # product's distinctive name in the opening content where the page title
    # and primary product details normally appear, not only in recommendations.
    opening_tokens = set(_tokens(content[:5000]))
    required = distinctive or product_tokens
    return any(token in opening_tokens for token in required)
