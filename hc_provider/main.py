import os
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

HARDCOVER_GRAPHQL_URL = "https://api.hardcover.app/v1/graphql"
HARDCOVER_TOKEN = os.environ.get(
    "HARDCOVER_TOKEN",
    "",
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

app = FastAPI(title="Hardcover Metadata Provider")


class SeriesMetadata(BaseModel):
    series: str
    sequence: Optional[str] = None


class BookMetadata(BaseModel):
    title: str
    subtitle: Optional[str] = None
    author: Optional[str] = None
    narrator: Optional[str] = None
    publisher: Optional[str] = None
    publishedYear: Optional[str] = None
    description: Optional[str] = None
    cover: Optional[str] = None
    isbn: Optional[str] = None
    asin: Optional[str] = None
    genres: Optional[list[str]] = None
    tags: Optional[list[str]] = None
    series: Optional[list[SeriesMetadata]] = None
    language: Optional[str] = None
    duration: Optional[int] = None


class SearchResponse(BaseModel):
    matches: list[BookMetadata]


_AUTHOR_ROLES = {"", "author", "written by"}
_NARRATOR_ROLES = {"narrator", "read by", "narrated by"}


def _build_where(query: str, author: Optional[str]) -> dict:
    filters = [
        {"title": {"_ilike": f"%{query}%"}},
        {"default_physical_edition": {"language": {"language": {"_is_null": False}}}},
    ]
    if author:
        filters.append(
            {"contributions": {"author": {"name": {"_ilike": f"%{author}%"}}}}
        )
    return {"_and": filters}


def _map_book(raw: dict) -> BookMetadata:
    contributions = raw.get("contributions") or []

    authors, narrators = [], []
    for c in contributions:
        person = c.get("author") or {}
        name = person.get("name")
        if not name:
            continue
        role = (c.get("contribution") or "").lower().strip()
        if role in _NARRATOR_ROLES:
            narrators.append(name)
        elif role in _AUTHOR_ROLES:
            authors.append(name)

    # Fall back: if no role-tagged authors, take everyone not a narrator
    if not authors:
        authors = [
            (c.get("author") or {}).get("name")
            for c in contributions
            if (c.get("contribution") or "").lower().strip() not in _NARRATOR_ROLES
            and (c.get("author") or {}).get("name")
        ]

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

    genres = [
        g["genre"]["genre"]
        for g in (raw.get("genres") or [])
        if (g.get("genre") or {}).get("genre")
    ]
    raw_tags = raw.get("cached_tags") or []
    if isinstance(raw_tags, list):
        tags = [t for t in raw_tags if isinstance(t, str)]
    else:
        tags = []

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
        genres=genres or None,
        tags=tags or None,
        series=series_list or None,
        language=(edition.get("language") or {}).get("language"),
    )


_HC_HEADERS = {
    "Content-Type": "application/json",
    "Authorization": HARDCOVER_TOKEN,
}


async def _query_books(client: httpx.AsyncClient, where: dict) -> list[dict]:
    payload = {
        "query": BOOKS_QUERY,
        "variables": {"where": where, "limit": 20},
    }
    try:
        resp = await client.post(
            HARDCOVER_GRAPHQL_URL, json=payload, headers=_HC_HEADERS
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502, detail=f"Hardcover API error: {exc.response.status_code}"
        )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Hardcover API unreachable: {exc}")

    data = resp.json()
    if "errors" in data:
        raise HTTPException(
            status_code=502, detail=f"Hardcover GraphQL error: {data['errors']}"
        )

    return (data.get("data") or {}).get("books") or []


@app.get("/search", response_model=SearchResponse)
async def search(
    query: str = Query(..., description="Search term"),
    author: Optional[str] = Query(None, description="Filter by author name"),
) -> SearchResponse:
    if not HARDCOVER_TOKEN:
        raise HTTPException(status_code=500, detail="HARDCOVER_TOKEN not configured")

    async with httpx.AsyncClient(timeout=15.0) as client:
        books_raw = await _query_books(client, _build_where(query, author))

        # Author filter may not match when a book is filed under a franchise
        # owner rather than the actual writer (e.g. Foundation books by Brin/Bear
        # shelved under Asimov). Fall back to title-only search in that case.
        if not books_raw and author:
            books_raw = await _query_books(client, _build_where(query, None))

    return SearchResponse(matches=[_map_book(b) for b in books_raw])
