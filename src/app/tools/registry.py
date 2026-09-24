"""Per-tool policy: kill switches and timeouts.

Scholarship Finder's list of things not to repeat includes "source adapters
with no per-source kill switch". A tool that starts returning garbage, or
whose provider starts billing unexpectedly, has to be stoppable by
configuration alone - without a deploy, and without taking the other tools
down with it.

Kill switches are checked at the point of use rather than at startup, so
flipping one takes effect on the next call.
"""

from dataclasses import dataclass

from app.core.config import get_settings
from app.tools.base import ToolDisabled

DIRECT_FETCH = "direct_fetch"
JINA = "jina"
#: Its own switch, not Jina's. They fail independently: the reader has
#: done every official-page read in the pipeline without an error, so a
#: search returning noise must be stoppable on its own.
JINA_SEARCH = "jina_search"
TAVILY = "tavily"
PARSEBOT = "parsebot"

#: Every tool this build knows about. Listing them explicitly means a typo
#: in DISABLED_TOOLS is caught rather than silently disabling nothing.
KNOWN_TOOLS = frozenset({DIRECT_FETCH, JINA, JINA_SEARCH, TAVILY, PARSEBOT})

#: Registered but not implemented until the harvest migration.
UNIMPLEMENTED_TOOLS = frozenset({TAVILY, PARSEBOT})


@dataclass(frozen=True)
class ToolSpec:
    name: str
    enabled: bool
    timeout_seconds: float


def unknown_disabled_tools() -> frozenset[str]:
    """Names in DISABLED_TOOLS that match no known tool.

    Surfaced rather than ignored: a misspelled kill switch reads as "this
    tool is off" to whoever set it, while the tool carries on running.
    """
    return frozenset(get_settings().disabled_tools) - KNOWN_TOOLS


def is_enabled(tool: str) -> bool:
    return tool not in get_settings().disabled_tools


def spec(tool: str) -> ToolSpec:
    settings = get_settings()
    timeouts = {
        DIRECT_FETCH: settings.fetch_timeout_seconds,
        JINA: settings.jina_timeout_seconds,
        JINA_SEARCH: settings.jina_timeout_seconds,
        TAVILY: settings.tavily_timeout_seconds,
        PARSEBOT: settings.parsebot_timeout_seconds,
    }
    return ToolSpec(
        name=tool,
        enabled=is_enabled(tool),
        timeout_seconds=timeouts.get(tool, settings.fetch_timeout_seconds),
    )


def require_enabled(tool: str) -> ToolSpec:
    """Return the tool's policy, or refuse if its kill switch is off."""
    resolved = spec(tool)
    if not resolved.enabled:
        raise ToolDisabled(f"tool {tool!r} is disabled by configuration")
    return resolved
