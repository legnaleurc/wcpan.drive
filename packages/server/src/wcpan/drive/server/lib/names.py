"""Name normalization for stored path components."""

import unicodedata


def normalize_name(name: str) -> str:
    """Return the normalized (NFC) form used for stored path components."""
    return unicodedata.normalize("NFC", name)
