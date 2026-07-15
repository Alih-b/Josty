from typing import Annotated, Literal

from fastapi import FastAPI, Query

from .engine import DeepSearch

app = FastAPI(title="Deep Search", version="0.2.0")
engine = DeepSearch()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "scope": "local-service-only"}


@app.get("/search")
async def search(
    q: Annotated[str, Query(min_length=2, max_length=500)],
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
    fetch: bool = False,
    research: bool = True,
    mode: Literal["plain", "exact", "oss"] = "plain",
    category: Literal["text", "news"] = "text",
    region: Annotated[str | None, Query(min_length=2, max_length=20)] = None,
    safesearch: Literal["on", "moderate", "off"] = "moderate",
    timelimit: Literal["d", "w", "m", "y"] | None = None,
    site: Annotated[list[str] | None, Query()] = None,
):
    options = {
        "sites": site,
        "mode": mode,
        "limit": limit,
        "fetch": fetch,
        "category": category,
        "region": region,
        "safesearch": safesearch,
        "timelimit": timelimit,
    }
    if research:
        run = await engine.research_run(q, **options)
    else:
        run = await engine.search_run(q, **options)
    return run.dict()
