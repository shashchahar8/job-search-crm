import pytest

from app.collectors.base import UnsupportedSourceError
from app.collectors.registry import get_collector, get_source_registration
from app.collectors.seek import SeekCollector
from app.config import Settings
from app.database import SessionLocal


def test_seek_is_registered_with_source_capabilities() -> None:
    registration = get_source_registration("seek")

    assert registration.enabled is True
    assert registration.supported is True
    assert registration.capabilities.supports_keyword_query is True
    assert registration.capabilities.requires_persistent_browser_profile is True
    assert registration.capabilities.supports_description_collection is True


def test_future_sources_are_known_but_not_collectable() -> None:
    registration = get_source_registration("prosple")

    assert registration.enabled is False
    assert registration.supported is False
    with pytest.raises(UnsupportedSourceError, match="not supported"):
        registration.build_collector(Settings(), SessionLocal)


def test_seek_collector_resolves_through_registry() -> None:
    collector = get_collector("seek", Settings(), SessionLocal)

    assert isinstance(collector, SeekCollector)
