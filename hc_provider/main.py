import os
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import sentry_sdk
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from hc_provider.genres import accepted_genres, clean_tags

HARDCOVER_GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
HARDCOVER_TOKEN = os.environ.get("HARDCOVER_TOKEN", "").strip()
USER_AGENT = "hc-provider (+https://github.com/cmathews393/hc_provider)"
RESULT_LIMIT = 20

# DSN, environment and release come from SENTRY_DSN, SENTRY_ENVIRONMENT and
# SENTRY_RELEASE; with no DSN set the SDK is a no-op. The FastAPI and httpx
# integrations are enabled automatically and trace incoming/outgoing requests.
sentry_sdk.init(
    traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
)

# Filtering with _ilike and friends is disabled for API tokens, so matching goes
# through Hardcover's search index and full details are fetched by id.
SEARCH_QUERY = """
query SearchBooks(
  $query: String!
  $perPage: Int!
  $fields: String
  $weights: String
  $typos: String
) {
  search(
    query: $query
    query_type: "book"
    per_page: $perPage
    fields: $fields
    weights: $weights
    typos: $typos
  ) {
    ids
    error
  }
}
"""

# Kept to three levels of nesting, ahead of Hardcover's planned depth limit.
BOOKS_QUERY = """
query BooksById($ids: [Int!]!) {
  books(where: {id: {_in: $ids}}) {
    id
    title
    subtitle
    description
    release_year
    audio_seconds
    image {
      url
    }
    contributions {
      contribution
      author {
        name
      }
    }
    book_series {
      position
      series {
        name
      }
    }
    default_physical_edition {
      isbn_13
      isbn_10
      language {
        language
      }
      publisher {
        name
      }
    }
    default_audio_edition {
      asin
      audio_seconds
      cached_contributors
      publisher {
        name
      }
    }
    cached_tags
  }
}
"""


class SeriesMetadata(BaseModel):
    series: str
    sequence: str | None = None


class BookMetadata(BaseModel):
    title: str
    subtitle: str | None = None
    author: str | None = None
    narrator: str | None = None
    publisher: str | None = None
    publishedYear: str | None = None
    description: str | None = None
    cover: str | None = None
    isbn: str | None = None
    asin: str | None = None
    genres: list[str] | None = None
    tags: list[str] | None = None
    series: list[SeriesMetadata] | None = None
    language: str | None = None
    duration: int | None = None


class SearchResponse(BaseModel):
    matches: list[BookMetadata]


_AUTHOR_ROLES = {"", "author", "written by"}
_NARRATOR_ROLES = {"narrator", "read by", "narrated by"}


def _auth_header(token: str) -> str:
    # Accept the token with or without the "Bearer " prefix
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return f"Bearer {token}"


def _matches_author(raw: dict, author: str) -> bool:
    wanted = [
        part.strip().lower()
        for part in re.split(r",|&|;| and ", author)
        if part.strip()
    ]
    names = [
        ((c.get("author") or {}).get("name") or "").lower()
        for c in raw.get("contributions") or []
    ]
    return any(w in n or n in w for w in wanted for n in names if n)


def _cached_tags(raw: dict, category: str) -> list[str]:
    # cached_tags is keyed by category, e.g. {"Genre": [{"tag": "Fantasy", ...}]}
    cached = raw.get("cached_tags")
    if not isinstance(cached, dict):
        return []
    return [
        t["tag"]
        for t in cached.get(category) or []
        if isinstance(t, dict) and isinstance(t.get("tag"), str)
    ]


def _map_book(raw: dict) -> BookMetadata:
    audio = raw.get("default_audio_edition") or {}

    authors: list[str] = []
    narrators: list[str] = []
    others: list[str] = []
    # Narrators are usually credited on the audio edition rather than the book
    for c in [
        *(raw.get("contributions") or []),
        *(audio.get("cached_contributors") or []),
    ]:
        if not isinstance(c, dict):
            continue
        name = (c.get("author") or {}).get("name")
        if not name:
            continue
        role = (c.get("contribution") or "").lower().strip()
        if role in _NARRATOR_ROLES:
            if name not in narrators:
                narrators.append(name)
        elif role in _AUTHOR_ROLES:
            if name not in authors:
                authors.append(name)
        elif name not in others:
            others.append(name)

    # Fall back: if no role-tagged authors, take everyone not a narrator
    if not authors:
        authors = others

    edition = raw.get("default_physical_edition") or {}
    publisher = (audio.get("publisher") or edition.get("publisher") or {}).get("name")

    series_list = [
        SeriesMetadata(
            series=bs["series"]["name"],
            sequence=str(bs["position"]) if bs.get("position") is not None else None,
        )
        for bs in (raw.get("book_series") or [])
        if (bs.get("series") or {}).get("name")
    ]

    image = raw.get("image") or {}
    audio_seconds = audio.get("audio_seconds") or raw.get("audio_seconds")

    return BookMetadata(
        title=raw["title"],
        subtitle=raw.get("subtitle") or None,
        author=", ".join(authors) if authors else None,
        narrator=", ".join(narrators) if narrators else None,
        publisher=publisher,
        publishedYear=str(raw["release_year"]) if raw.get("release_year") else None,
        description=raw.get("description"),
        cover=image.get("url"),
        isbn=edition.get("isbn_13") or edition.get("isbn_10"),
        asin=audio.get("asin") or None,
        genres=accepted_genres(_cached_tags(raw, "Genre")) or None,
        tags=clean_tags(_cached_tags(raw, "Tag")) or None,
        series=series_list or None,
        language=(edition.get("language") or {}).get("language"),
        # Audiobookshelf expects minutes
        duration=round(audio_seconds / 60) if audio_seconds else None,
    )


def _upstream_error(resp: httpx.Response) -> HTTPException:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {}
    reason = (
        body.get("error_description")
        or body.get("message")
        or body.get("error")
        or resp.reason_phrase
    )

    match resp.status_code:
        case 401:
            detail = (
                f"Hardcover rejected HARDCOVER_TOKEN (invalid or expired): {reason}"
            )
            return HTTPException(status_code=502, detail=detail)
        case 403:
            if scope := body.get("scope"):
                reason = f"{reason} (token needs scope: {scope})"
            return HTTPException(
                status_code=502, detail=f"Hardcover refused the request: {reason}"
            )
        case 429:
            retry_after = resp.headers.get("Retry-After")
            return HTTPException(
                status_code=429,
                detail="Hardcover rate limit reached",
                headers={"Retry-After": retry_after} if retry_after else None,
            )
        case 408:
            return HTTPException(status_code=504, detail="Hardcover query timed out")
        case status:
            return HTTPException(
                status_code=502, detail=f"Hardcover API error {status}: {reason}"
            )


async def _graphql(
    client: httpx.AsyncClient, name: str, query: str, variables: dict
) -> dict:
    with sentry_sdk.start_span(op="graphql.query", name=name) as span:
        span.set_data("graphql.variables", variables)
        try:
            resp = await client.post(
                HARDCOVER_GRAPHQL_URL, json={"query": query, "variables": variables}
            )
        except httpx.TimeoutException as exc:
            raise HTTPException(
                status_code=504, detail="Hardcover API timed out"
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"Hardcover API unreachable: {exc}"
            ) from exc
        if resp.is_error:
            raise _upstream_error(resp)

        data = resp.json()
        if "errors" in data:
            raise HTTPException(
                status_code=502, detail=f"Hardcover GraphQL error: {data['errors']}"
            )
        return data.get("data") or {}


async def _search_ids(
    client: httpx.AsyncClient, query: str, *, isbn: bool = False
) -> list[int]:
    variables: dict = {"query": query, "perPage": RESULT_LIMIT}
    if isbn:
        # Exact match on ISBNs only, so digits don't fuzzy-match titles
        variables |= {"fields": "isbns", "weights": "1", "typos": "0"}
    data = await _graphql(client, "SearchBooks", SEARCH_QUERY, variables)
    result = data.get("search") or {}
    if result.get("error"):
        raise HTTPException(
            status_code=502, detail=f"Hardcover search error: {result['error']}"
        )
    return [i for i in result.get("ids") or [] if i is not None]


async def _fetch_books(client: httpx.AsyncClient, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    data = await _graphql(client, "BooksById", BOOKS_QUERY, {"ids": ids})
    by_id = {b["id"]: b for b in data.get("books") or []}
    # Keep the search ranking
    return [by_id[i] for i in ids if i in by_id]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with httpx.AsyncClient(
        timeout=15.0,
        headers={
            "Authorization": _auth_header(HARDCOVER_TOKEN),
            "User-Agent": USER_AGENT,
        },
    ) as client:
        app.state.client = client
        yield


app = FastAPI(title="Hardcover Metadata Provider", lifespan=_lifespan)


@app.get("/search", response_model=SearchResponse)
async def search(
    query: str = Query(..., description="Search term"),
    author: str | None = Query(None, description="Filter by author name"),
    isbn: str | None = Query(None, description="Look up by ISBN first"),
) -> SearchResponse:
    if not HARDCOVER_TOKEN:
        raise HTTPException(status_code=500, detail="HARDCOVER_TOKEN not configured")

    client: httpx.AsyncClient = app.state.client

    # An ISBN hit is exact, so skip the title search and author filter
    ids = await _search_ids(client, isbn, isbn=True) if isbn else []
    sentry_sdk.set_tag("search.isbn_hit", bool(ids))
    books_raw = await _fetch_books(client, ids)

    if not books_raw:
        books_raw = await _fetch_books(client, await _search_ids(client, query))

        # Author filter may not match when a book is filed under a franchise
        # owner rather than the actual writer (e.g. Foundation books by Brin/Bear
        # shelved under Asimov). Keep the unfiltered results in that case.
        if author:
            by_author = [b for b in books_raw if _matches_author(b, author)]
            sentry_sdk.set_tag("search.author_fallback", not by_author)
            books_raw = by_author or books_raw

    with sentry_sdk.start_span(op="function", name="map_books"):
        matches = [_map_book(b) for b in books_raw]
    return SearchResponse(matches=matches)
