"""
upn.py — turning a human name into a usable userPrincipalName.

Two problems, both of which bite in production:

  1. Names contain characters a UPN cannot. Zoe Muller-Sondergaard is
     spelled Zoë Müller-Søndergaard in HR. Unicode normalization handles
     most of it; a handful of letters do not decompose and need an
     explicit map.

  2. Two people generate the same candidate. Daniel Okonkwo and
     Delphine Okonkwo both reduce to d.okonkwo. Something has to decide
     who gets it, and the loser needs a deterministic alternative.
"""

import unicodedata

# Characters NFKD will not decompose into base letter + combining mark.
# Without these, the stripping pass drops them entirely and you get
# UPNs like "mller-sndergaard".
SPECIAL = {
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ß": "ss",
    "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D",
    "ł": "l", "Ł": "L",
    "þ": "th", "Þ": "TH",
    "ı": "i",
}

ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-.")


def transliterate(text):
    """Unicode name -> plain ASCII, preserving as much as possible."""
    for char, replacement in SPECIAL.items():
        text = text.replace(char, replacement)
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed
                       if not unicodedata.combining(c))
    return stripped.encode("ascii", "ignore").decode("ascii")


def sanitize(text):
    """Lowercase, drop anything a UPN local part cannot carry."""
    text = transliterate(text).lower()
    cleaned = "".join(c if c in ALLOWED else "" for c in text)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned.strip(".-")


def candidates(first_name, last_name, limit=20):
    """
    Deterministic ladder of local parts, best first.

    Deterministic matters: the same worker must generate the same
    candidate list on every run, or a rerun invents a new UPN for
    someone who already has one.
    """
    first = sanitize(first_name)
    last = sanitize(last_name)
    if not first or not last:
        raise ValueError(f"name unusable after sanitizing: "
                         f"{first_name!r} {last_name!r}")

    base = f"{first[0]}.{last}"
    yield base
    # Then the full first name, which usually breaks a two-person tie.
    if first != first[0]:
        yield f"{first}.{last}"
    # Then numeric suffixes, so the ladder never runs out.
    for n in range(2, limit):
        yield f"{base}{n}"


def generate(first_name, last_name, upn_suffix, taken):
    """
    First candidate not already in `taken`.

    `taken` is a set of lowercased UPNs - both what exists in the
    directory and what this run has already allocated. Leave out the
    second part and two new hires in the same batch collide with each
    other rather than with the directory.

    Returns (upn, collided) so the caller can log the collision. The
    collision is worth logging: it is how you find out two real people
    are competing for an identifier.
    """
    collided = False
    for local in candidates(first_name, last_name):
        upn = f"{local}@{upn_suffix}".lower()
        if upn not in taken:
            return upn, collided
        collided = True
    raise RuntimeError(
        f"no available UPN for {first_name} {last_name} after 20 attempts"
    )


def mail_nickname(upn):
    return upn.split("@", 1)[0]
