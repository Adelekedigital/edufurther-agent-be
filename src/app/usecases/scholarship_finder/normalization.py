"""Identity keys, ported verbatim from Scholarship Finder.

Verbatim is the requirement, not a convenience. The product computes
`normalized_identity_key` this way and uses it to decide whether two
discoveries are the same award; if the agent computed keys even slightly
differently, its idea of a duplicate and the product's would diverge, and
the agent would submit candidates the product then re-splits or re-merges.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedDiscovery:
    title: str
    identity_key: str
    tokens: tuple[str, ...]


def normalize_discovery(title: str, provider: str | None = None) -> NormalizedDiscovery:
    clean_title = re.sub(r"\s+", " ", title.strip()).casefold()
    clean_provider = re.sub(r"\s+", " ", (provider or "").strip()).casefold()
    tokens = tuple(sorted(set(re.findall(r"[a-z0-9]+", f"{clean_provider} {clean_title}"))))
    identity_key = "|".join(tokens)
    return NormalizedDiscovery(clean_title, identity_key, tokens)
