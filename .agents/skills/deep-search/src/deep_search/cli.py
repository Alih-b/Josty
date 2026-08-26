import argparse
import asyncio
import json
import os
import sys

from .engine import DeepSearch


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Small, auditable metasearch for AI agents"
    )
    command.add_argument("query", help="search query")
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--site", action="append", dest="sites", help="add a site: filter")
    command.add_argument("--mode", choices=("plain", "exact", "oss"), default="plain")
    command.add_argument("--category", choices=("text", "news"), default="text")
    command.add_argument("--region", help="DDGS region code, for example us-en or de-de")
    command.add_argument(
        "--safe-search", choices=("on", "moderate", "off"), default="moderate"
    )
    command.add_argument(
        "--time-limit", choices=("d", "w", "m", "y"), help="day, week, month, or year"
    )
    command.add_argument("--fetch", action="store_true", help="extract bounded text from results")
    command.add_argument(
        "--github", action="store_true", help="also search official GitHub repositories"
    )
    command.add_argument(
        "--results-only", action="store_true", help="emit only the result array"
    )
    command.add_argument(
        "--search-concurrency",
        type=int,
        default=DeepSearch.DEFAULT_SEARCH_CONCURRENCY,
        help="max concurrent search backend requests (default: %(default)d)",
    )
    command.add_argument(
        "--fetch-concurrency",
        type=int,
        default=DeepSearch.DEFAULT_FETCH_CONCURRENCY,
        help="max concurrent page fetches (default: %(default)d)",
    )
    return command


async def run(args: argparse.Namespace) -> dict | list:
    engine = DeepSearch(
        github_token=os.getenv("GITHUB_TOKEN"),
        max_search_concurrency=args.search_concurrency,
        max_fetch_concurrency=args.fetch_concurrency,
    )
    search = await engine.research_run(
        args.query,
        sites=args.sites,
        mode=args.mode,
        limit=args.limit,
        fetch=args.fetch,
        include_github=args.github,
        category=args.category,
        region=args.region,
        safesearch=args.safe_search,
        timelimit=args.time_limit,
    )
    return [item.dict() for item in search.results] if args.results_only else search.dict()


def main() -> None:
    args = parser().parse_args()
    try:
        payload = asyncio.run(run(args))
    except (ValueError, KeyboardInterrupt) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
