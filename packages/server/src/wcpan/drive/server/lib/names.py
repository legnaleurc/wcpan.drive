"""Name handling for Synology Drive API calls."""

import unicodedata


def normalize_name(name: str) -> str:
    """Return the form Synology Drive uses for stored path components."""
    return unicodedata.normalize("NFC", name)
