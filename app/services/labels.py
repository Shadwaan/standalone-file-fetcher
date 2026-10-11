"""Version labels on titles.

A download is sometimes a different version of the song than the one in the Spotify playlist
(another mix) and is kept on purpose. It must say so in Rekordbox or it misleads: the title gets
a trailing "[label]", e.g.  I Feel For You [CZR's Peak Hour Mix].

The rest of sff compares titles to decide whether a track is already in a playlist, so every
comparison goes through `title_key`, which ignores a trailing label.
"""
import re

_LABEL = re.compile(r"\s*\[[^\[\]]+\]\s*$")


def strip_label(title: str) -> str:
    """The title without a trailing [label]."""
    return _LABEL.sub("", title or "").strip()


_BRACKETED = re.compile(r"\[([^\[\]]+)\]")


def clean_label(label: str) -> str:
    """A label as it will be shown. When the text has a [bracketed] part, that part is the label (the rest is the
    version it was downloaded as, e.g. "Back & Forth (Original Mix) [Hot Creations]" -> "Hot Creations"); any
    other brackets become round ones so they can't be mistaken for the label's own."""
    label = label or ""
    found = _BRACKETED.findall(label)
    if found:
        label = found[-1]
    return " ".join(label.replace("[", "(").replace("]", ")").split())


def with_label(title: str, label: str) -> str:
    """`title` carrying `label` (replacing any label it already has); no label means the plain title."""
    base = strip_label(title)
    label = clean_label(label)
    return f"{base} [{label}]" if label else base


def title_key(title: str) -> str:
    """What titles are compared by."""
    return strip_label(title)


_FEAT = re.compile(r"[\(\[]\s*(?:feat\.?|ft\.?|featuring)\b[^\)\]]*[\)\]]|\b(?:feat\.?|ft\.?|featuring)\b.*$", re.I)


def norm_title(title: str) -> str:
    """A title for comparing with files that spell it a little differently: [Dub] and (Dub) are the same, a
    "(feat. X)" credit is ignored, case and punctuation do not matter. The VERSION words stay, so
    "Oh Mama (Dub)" is still not "Oh Mama"."""
    t = _FEAT.sub(" ", title or "")
    t = re.sub(r"['\u2019`]", "", t)                      # don't -> dont, not "don t"
    t = t.replace("[", " ").replace("]", " ").replace("(", " ").replace(")", " ")
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return " ".join(t.split())
