"""Per-tool kill switches.

Scholarship Finder's list of things not to repeat includes "source adapters
with no per-source kill switch". A tool returning garbage, or a provider
billing unexpectedly, has to be stoppable by configuration alone.
"""

import pytest

from app.core.config import get_settings
from app.tools import registry
from app.tools.base import ToolDisabled


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", set())


def test_tools_are_enabled_by_default():
    assert registry.is_enabled(registry.DIRECT_FETCH)
    assert registry.is_enabled(registry.JINA)


def test_disabling_one_tool_leaves_the_others_running(monkeypatch):
    """The point of per-tool switches: stopping Jina must not stop
    fetching."""
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.JINA})

    assert not registry.is_enabled(registry.JINA)
    assert registry.is_enabled(registry.DIRECT_FETCH)


def test_requiring_a_disabled_tool_refuses(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.TAVILY})

    with pytest.raises(ToolDisabled, match="tavily"):
        registry.require_enabled(registry.TAVILY)


def test_requiring_an_enabled_tool_returns_its_policy():
    spec = registry.require_enabled(registry.DIRECT_FETCH)

    assert spec.name == registry.DIRECT_FETCH
    assert spec.enabled
    assert spec.timeout_seconds > 0


def test_a_switch_takes_effect_without_a_restart(monkeypatch):
    """Checked at the point of use, not read once at startup - otherwise
    stopping a misbehaving tool needs a deploy."""
    assert registry.is_enabled(registry.JINA)
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.JINA})
    assert not registry.is_enabled(registry.JINA)


@pytest.mark.parametrize(
    "tool", [registry.DIRECT_FETCH, registry.JINA, registry.TAVILY, registry.PARSEBOT]
)
def test_every_known_tool_has_its_own_timeout(tool):
    """A shared timeout would make a slow marketplace API and a page fetch
    the same problem, and they are not."""
    assert registry.spec(tool).timeout_seconds > 0


def test_a_misspelled_kill_switch_is_reported(monkeypatch):
    """The dangerous failure: whoever set it believes the tool is off while
    it carries on running."""
    monkeypatch.setattr(get_settings(), "disabled_tools", {"jinja", registry.JINA})

    assert registry.unknown_disabled_tools() == frozenset({"jinja"})
    assert not registry.is_enabled(registry.JINA)


def test_a_correct_kill_switch_reports_nothing_unknown(monkeypatch):
    monkeypatch.setattr(get_settings(), "disabled_tools", {registry.JINA})

    assert registry.unknown_disabled_tools() == frozenset()


def test_the_unimplemented_tools_are_still_registered():
    """Tavily and Parse.bot arrive with the harvest migration. Registering
    them now means the kill switch and timeout policy already exist when
    they do, rather than being retrofitted."""
    assert registry.UNIMPLEMENTED_TOOLS <= registry.KNOWN_TOOLS
    assert registry.TAVILY in registry.UNIMPLEMENTED_TOOLS
    assert registry.PARSEBOT in registry.UNIMPLEMENTED_TOOLS
