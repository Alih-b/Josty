"""ddgs engine registry access and availability checks."""

from __future__ import annotations

from .status import SearchCategory

try:
    from ddgs.engines import ENGINES as _DDGS_ENGINES
    try:
        # Re-register Google when an installed ddgs dropped it from its engine
        # table (9.15.0 deprecation). This mutates the ddgs global engine map
        # for the whole process — deliberate, since josty is the process's
        # search layer. Any layout change upstream is swallowed: registration
        # is best-effort, never a startup failure.
        from ddgs.engines.google import Google as _GoogleEngine

        if (
            _DDGS_ENGINES is not None
            and "text" in _DDGS_ENGINES
            and "google" not in _DDGS_ENGINES["text"]
        ):
            _DDGS_ENGINES["text"]["google"] = _GoogleEngine
    except Exception:
        pass
except Exception:
    _DDGS_ENGINES = None

_KNOWN_TEXT_ENGINES = frozenset(
    {
        "bing",
        "brave",
        "duckduckgo",
        "google",
        "grokipedia",
        "mojeek",
        "startpage",
        "wikipedia",
        "yahoo",
        "yandex",
    }
)
_KNOWN_NEWS_ENGINES = frozenset({"bing", "duckduckgo", "yahoo"})
# Both frozensets are a best-effort fallback for the case where the
# ddgs.engines registry cannot be imported at all (non-standard ddgs layout).
# They can drift from ddgs releases; the live registry is authoritative
# whenever the import succeeds.


def _engine_available(category: SearchCategory, name: str) -> tuple[bool, str | None]:
    """Check engine availability without calling ddgs.

    ddgs silently drops unknown or disabled engine names inside a group call and
    falls back to ``backend="auto"`` (all engines) when none match — a silent
    amplification and downgrade trap. Checking availability here lets a dead or
    misspelled engine be skipped with a visible status instead.
    """
    if _DDGS_ENGINES is not None:
        if name in _DDGS_ENGINES.get(category, {}):
            return True, None
        return False, f"skipped: engine '{name}' is not enabled in the installed ddgs"
    known = _KNOWN_NEWS_ENGINES if category == "news" else _KNOWN_TEXT_ENGINES
    if name in known:
        return True, None
    return False, f"skipped: unknown engine '{name}'"
