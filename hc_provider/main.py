import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import sentry_sdk
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

HARDCOVER_GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
HARDCOVER_TOKEN = os.environ.get("HARDCOVER_TOKEN", "")
RESULT_LIMIT = 20

# DSN, environment and release come from SENTRY_DSN, SENTRY_ENVIRONMENT and
# SENTRY_RELEASE; with no DSN set the SDK is a no-op. The FastAPI and httpx
# integrations are enabled automatically and trace incoming/outgoing requests.
sentry_sdk.init(
    traces_sample_rate=float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
)

BOOKS_QUERY = """
query SearchBooks($where: books_bool_exp!, $limit: Int!) {
  books(where: $where, limit: $limit, order_by: [{users_count: desc_nulls_last}]) {
    id
    title
    subtitle
    description
    release_year
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


def _build_where(query: str, author: str | None) -> dict:
    filters = [
        {"title": {"_ilike": f"%{query}%"}},
        {"default_physical_edition": {"language": {"language": {"_is_null": False}}}},
    ]
    if author:
        filters.append(
            {"contributions": {"author": {"name": {"_ilike": f"%{author}%"}}}}
        )
    return {"_and": filters}


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
    authors: list[str] = []
    narrators: list[str] = []
    others: list[str] = []
    for c in raw.get("contributions") or []:
        name = (c.get("author") or {}).get("name")
        if not name:
            continue
        role = (c.get("contribution") or "").lower().strip()
        if role in _NARRATOR_ROLES:
            narrators.append(name)
        elif role in _AUTHOR_ROLES:
            authors.append(name)
        else:
            others.append(name)

    # Fall back: if no role-tagged authors, take everyone not a narrator
    if not authors:
        authors = others

    edition = raw.get("default_physical_edition") or {}
    publisher_obj = edition.get("publisher") or {}

    series_list = [
        SeriesMetadata(
            series=bs["series"]["name"],
            sequence=str(bs["position"]) if bs.get("position") is not None else None,
        )
        for bs in (raw.get("book_series") or [])
        if (bs.get("series") or {}).get("name")
    ]

    image = raw.get("image") or {}

    return BookMetadata(
        title=raw["title"],
        subtitle=raw.get("subtitle") or None,
        author=", ".join(authors) if authors else None,
        narrator=", ".join(narrators) if narrators else None,
        publisher=publisher_obj.get("name"),
        publishedYear=str(raw["release_year"]) if raw.get("release_year") else None,
        description=raw.get("description"),
        cover=image.get("url"),
        isbn=edition.get("isbn_13") or edition.get("isbn_10"),
        genres=_cached_tags(raw, "Genre") or None,
        tags=_cached_tags(raw, "Tag") or None,
        series=series_list or None,
        language=(edition.get("language") or {}).get("language"),
    )


async def _query_books(client: httpx.AsyncClient, where: dict) -> list[dict]:
    payload = {
        "query": BOOKS_QUERY,
        "variables": {"where": where, "limit": RESULT_LIMIT},
    }
    with sentry_sdk.start_span(op="graphql.query", name="SearchBooks") as span:
        span.set_data("graphql.variables", payload["variables"])
        try:
            resp = await client.post(HARDCOVER_GRAPHQL_URL, json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Hardcover API error: {exc.response.status_code}",
            ) from exc
        except httpx.RequestError as exc:
            raise HTTPException(
                status_code=502, detail=f"Hardcover API unreachable: {exc}"
            ) from exc

        data = resp.json()
        if "errors" in data:
            raise HTTPException(
                status_code=502, detail=f"Hardcover GraphQL error: {data['errors']}"
            )

        books = (data.get("data") or {}).get("books") or []
        span.set_data("result_count", len(books))
        return books


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None]:
    async with httpx.AsyncClient(
        timeout=15.0, headers={"Authorization": HARDCOVER_TOKEN}
    ) as client:
        app.state.client = client
        yield


app = FastAPI(title="Hardcover Metadata Provider", lifespan=_lifespan)


@app.get("/search", response_model=SearchResponse)
async def search(
    query: str = Query(..., description="Search term"),
    author: str | None = Query(None, description="Filter by author name"),
) -> SearchResponse:
    if not HARDCOVER_TOKEN:
        raise HTTPException(status_code=500, detail="HARDCOVER_TOKEN not configured")

    client: httpx.AsyncClient = app.state.client
    books_raw = await _query_books(client, _build_where(query, author))

    # Author filter may not match when a book is filed under a franchise
    # owner rather than the actual writer (e.g. Foundation books by Brin/Bear
    # shelved under Asimov). Fall back to title-only search in that case.
    fallback = not books_raw and bool(author)
    sentry_sdk.set_tag("search.author_fallback", fallback)
    if fallback:
        books_raw = await _query_books(client, _build_where(query, None))

    with sentry_sdk.start_span(op="function", name="map_books"):
        matches = [_map_book(b) for b in books_raw]
    return SearchResponse(matches=matches)
