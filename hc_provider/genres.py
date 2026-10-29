import re
import unicodedata
from collections.abc import Iterable
from difflib import get_close_matches

ACCEPTED_GENRES = (
    "Action/Adventure",
    "Art/Photography",
    "Biographical",
    "Business/Money",
    "Comedy",
    "Computer Science",
    "Contemporary Fiction",
    "Fiction",
    "Nonfiction",
    "Crafts",
    "Crime",
    "Education",
    "Textbook",
    "Erotica",
    "Fantasy",
    "Food",
    "Health",
    "Home/Garden",
    "Historical",
    "Horror",
    "Humor",
    "Juvenile",
    "Language",
    "LGBT",
    "Literary Fiction",
    "Classics",
    "LitRPG",
    "Math",
    "Military",
    "War",
    "Medicine",
    "Psychology",
    "Sociology",
    "Music",
    "Mystery",
    "Nature",
    "Occult",
    "Paranormal",
    "Philosophy",
    "Poetry",
    "Politics",
    "Reference",
    "Religion",
    "Romance",
    "RPG",
    "Science",
    "Science Fiction",
    "Self-Help",
    "Sports",
    "Hobbies",
    "Superheroes",
    "Technology",
    "Thriller",
    "Suspense",
    "Travel",
    "Urban Fantasy",
    "Western",
    "Young Adult",
)


# Other names Hardcover uses for genres on the list
GENRE_ALIASES = {
    "Biography": "Biographical",
    "Contemporary": "Contemporary Fiction",
    "Historical Fiction": "Historical",
    "Sci-Fi": "Science Fiction",
}

# Below this similarity a name is treated as a different genre, not a typo
_TYPO_CUTOFF = 0.88
# Short names are too close together to typo-match, e.g. "War" and "Wars"
_TYPO_MIN_LENGTH = 5


def _key(name: str) -> str:
    """Reduce a genre name to a comparable form.

    Drops case, accents, quotes and other punctuation, and ignores spacing,
    hyphens and underscores, so "Self-Help", "self help" and '"SelfHelp"' all
    compare equal. "&" is read as "/" so "Home & Garden" matches "Home/Garden".
    """
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.casefold().replace("&", "/")
    name = re.sub(r"[^a-z0-9/+]", "", name)
    return re.sub(r"/+", "/", name).strip("/")


_LOOKUP: dict[str, str] = {}
for _genre in ACCEPTED_GENRES:
    _LOOKUP[_key(_genre)] = _genre
for _alias, _genre in GENRE_ALIASES.items():
    _LOOKUP[_key(_alias)] = _genre
for _genre in ACCEPTED_GENRES:
    if "/" in _genre:
        # Either half of a combined genre maps to it, e.g. "Adventure" -> "Action/Adventure"
        for _part in _genre.split("/"):
            _LOOKUP.setdefault(_key(_part), _genre)

# LGBT plus extensions such as LGBTQ, LGBTQ+ and LGBTQIA+
_LGBT = re.compile(r"lgbt[a-z0-9+]*")


def _match(name: str) -> str | None:
    key = _key(name)
    if not key:
        return None
    if genre := _LOOKUP.get(key):
        return genre
    if _LGBT.fullmatch(key):
        return key.upper()
    if len(key) >= _TYPO_MIN_LENGTH:
        close = get_close_matches(key, _LOOKUP, n=1, cutoff=_TYPO_CUTOFF)
        if close:
            return _LOOKUP[close[0]]
    return None


def accepted_genres(names: Iterable[str]) -> list[str]:
    """Map Hardcover genres onto ACCEPTED_GENRES, dropping anything else."""
    genres: list[str] = []
    for name in names:
        genre = _match(name)
        if genre and genre not in genres:
            genres.append(genre)
    return genres


def _clean_tag(name: str) -> str:
    """Tidy a free-form tag for display.

    Removes quotes (keeping apostrophes inside words, as in "Women's Fiction"),
    unifies dash variants, drops other punctuation and tightens spacing, so
    ' "Time – Travel" ' becomes "Time-Travel".
    """
    name = unicodedata.normalize("NFKC", name)
    name = re.sub(r"[‘’´`]", "'", name)
    name = re.sub(r"(?<!\w)'|'(?!\w)", "", name)
    name = re.sub(r"[‐‑‒–—―]", "-", name)
    name = name.replace(".", "")  # "A.I." -> "AI"
    name = re.sub(r"[^\w\s'/&+-]|_", " ", name)
    name = re.sub(r"\s*-+\s*", "-", name)
    name = re.sub(r"\s*/+\s*", "/", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip(" -/&+")


def clean_tags(names: Iterable[str]) -> list[str]:
    """Clean up tags and drop duplicates, including close misspellings.

    The first spelling of each tag is kept.
    """
    tags: list[str] = []
    keys: list[str] = []
    for name in names:
        tag = _clean_tag(name)
        key = _key(tag)
        if not key or key in keys:
            continue
        if len(key) >= _TYPO_MIN_LENGTH and get_close_matches(
            key, keys, n=1, cutoff=_TYPO_CUTOFF
        ):
            continue
        tags.append(tag)
        keys.append(key)
    return tags
