"""The rules that decide whether a model number the AI named is really on the page.

A model is kept only if its key (letters and digits, uppercase) is at least 5 characters and
appears in the text that was sent. Anything else is dropped and never reaches the equipment tables.
"""
import re

from crawler.store import model_key

MIN_KEY_LENGTH = 5


def _alnum(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def verify(models, texts):
    """The model numbers that pass, in the answer's order, each once."""
    page_alnum = _alnum("\n".join(texts))
    kept, seen = [], set()
    for number in models:
        key = model_key(number)
        if len(key) >= MIN_KEY_LENGTH and key in page_alnum and key not in seen:
            seen.add(key)
            kept.append(number)
    return kept
