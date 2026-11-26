"""ddgs engine registry access and availability checks."""

from __future__ import annotations

from .status import SearchCategory

try:
    from ddgs.engines import ENGINES as _DDGS_ENGINES
except Exception:
    _DDGS_ENGINES = None
else:
    # ddgs builds ENGINES once, at import, and drops every class flagged
    # ``disabled``; setting the flag afterwards is too late, and ``backend="yandex"``
    # would silently fall back to ``"auto"``. Register the installed classes here,
    # before the first DDGS() call. This is an allow-list, not "un-disable
    # everything": Google stays out on purpose. A ddgs release that drops one of
    # these classes fails this import loudly, which beats a skip nobody asked for.
    from ddgs.engines.bing import Bing
    from ddgs.engines.yandex import Yandex

    _DDGS_ENGINES["text"]["bing"] = Bing
    _DDGS_ENGINES["text"]["yandex"] = Yandex

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
