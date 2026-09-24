"""Identity keys, ported verbatim from Scholarship Finder.

Verbatim is the requirement, not a convenience. The product computes
`normalized_identity_key` this way and uses it to decide whether two
discoveries are the same award; if the agent computed keys even slightly
differently, its idea of a duplicate and the product's would diverge, and
the agent would submit candidates the product then re-splits or re-merges.
"""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedDiscovery:
    title: str
    identity_key: str
    tokens: tuple[str, ...]


#: Characters that are invisible when rendered but split a word when the
#: token pattern runs. A page carrying "Max We<shy>ber" looks identical to
#: "Max Weber" and keys differently.
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍﻿"))
#: Letters NFKD cannot help with, because they are distinct letters
#: rather than a base plus a combining mark. Skodowska keyed as
#: `sk|odowska` until the stroked l was mapped explicitly.
_INDIVISIBLE = str.maketrans(
    {
        "ł": "l",
        "Ł": "L",
        "ø": "o",
        "Ø": "O",
        "đ": "d",
        "Đ": "D",
        "æ": "ae",
        "Æ": "AE",
        "œ": "oe",
        "Œ": "OE",
        "þ": "th",
        "Þ": "TH",
        "ð": "d",
        "Ð": "D",
        "ß": "ss",
    }
)


def fold(value: str) -> str:
    """Strip invisibles and reduce accented letters to their base form.

    The token pattern is `[a-z0-9]+`, so any character outside it acts as a
    separator rather than being ignored. That fragments a name instead of
    merely altering it: "Koc" keyed as `ko`, "Boll" as `b|ll`, "Skodowska"
    as `sk|odowska`. A German or French award therefore had an identity key
    made of the pieces between its accents, and the same award reported
    without accents by another source never matched it.

    Decomposing and dropping the combining marks keeps the word whole.
    Measured over 1,808 live discoveries: 30 keys repaired, and - checked
    explicitly, because widening is the dangerous direction - zero
    previously distinct groups merged.
    """
    stripped = value.translate(_INVISIBLE).translate(_INDIVISIBLE)
    decomposed = unicodedata.normalize("NFKD", stripped)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_discovery(title: str, provider: str | None = None) -> NormalizedDiscovery:
    clean_title = re.sub(r"\s+", " ", fold(title).strip()).casefold()
    clean_provider = re.sub(r"\s+", " ", fold(provider or "").strip()).casefold()
    tokens = tuple(sorted(set(re.findall(r"[a-z0-9]+", f"{clean_provider} {clean_title}"))))
    identity_key = "|".join(tokens)
    return NormalizedDiscovery(clean_title, identity_key, tokens)
