from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.collectors.base import Collector, UnsupportedSourceError
from app.config import Settings


class SourceIdentifier(StrEnum):
    SEEK = "seek"
    PROSPLE = "prosple"
    SEEK_GRAD = "seek_grad"
    LINKEDIN = "linkedin"


@dataclass(frozen=True)
class SourceCapabilities:
    supports_keyword_query: bool
    supports_location: bool
    supports_date_window: bool
    supports_page_limit: bool
    requires_persistent_browser_profile: bool
    requires_manual_login_session_preparation: bool
    supports_resume_after_awaiting_user: bool
    supports_description_collection: bool
    supports_posted_date: bool
    supports_closing_date: bool
    supports_graduate_program_metadata: bool


@dataclass(frozen=True)
class CollectorRegistration:
    source_identifier: SourceIdentifier
    display_name: str
    enabled: bool
    supported: bool
    capabilities: SourceCapabilities
    collector_factory: Callable[[Settings, type[Session]], Collector] | None = None

    def build_collector(self, settings: Settings, session_factory: type[Session]) -> Collector:
        if not self.enabled or not self.supported or self.collector_factory is None:
            raise UnsupportedSourceError(unsupported_source_message(self.source_identifier.value))
        return self.collector_factory(settings, session_factory)


SEEK_CAPABILITIES = SourceCapabilities(
    supports_keyword_query=True,
    supports_location=True,
    supports_date_window=True,
    supports_page_limit=True,
    requires_persistent_browser_profile=True,
    requires_manual_login_session_preparation=True,
    supports_resume_after_awaiting_user=True,
    supports_description_collection=True,
    supports_posted_date=True,
    supports_closing_date=False,
    supports_graduate_program_metadata=False,
)

UNKNOWN_FUTURE_CAPABILITIES = SourceCapabilities(
    supports_keyword_query=False,
    supports_location=False,
    supports_date_window=False,
    supports_page_limit=False,
    requires_persistent_browser_profile=False,
    requires_manual_login_session_preparation=False,
    supports_resume_after_awaiting_user=False,
    supports_description_collection=False,
    supports_posted_date=False,
    supports_closing_date=False,
    supports_graduate_program_metadata=False,
)


def _seek_factory(settings: Settings, session_factory: type[Session]) -> Collector:
    from app.collectors.seek import SeekCollector

    return SeekCollector(settings, session_factory)


COLLECTOR_REGISTRY: dict[str, CollectorRegistration] = {
    SourceIdentifier.SEEK.value: CollectorRegistration(
        source_identifier=SourceIdentifier.SEEK,
        display_name="SEEK",
        enabled=True,
        supported=True,
        capabilities=SEEK_CAPABILITIES,
        collector_factory=_seek_factory,
    ),
    SourceIdentifier.PROSPLE.value: CollectorRegistration(
        source_identifier=SourceIdentifier.PROSPLE,
        display_name="Prosple",
        enabled=False,
        supported=False,
        capabilities=UNKNOWN_FUTURE_CAPABILITIES,
    ),
    SourceIdentifier.SEEK_GRAD.value: CollectorRegistration(
        source_identifier=SourceIdentifier.SEEK_GRAD,
        display_name="SEEK Grad",
        enabled=False,
        supported=False,
        capabilities=UNKNOWN_FUTURE_CAPABILITIES,
    ),
    SourceIdentifier.LINKEDIN.value: CollectorRegistration(
        source_identifier=SourceIdentifier.LINKEDIN,
        display_name="LinkedIn",
        enabled=False,
        supported=False,
        capabilities=UNKNOWN_FUTURE_CAPABILITIES,
    ),
}


def normalize_source_identifier(source_identifier: str | None) -> str:
    return (source_identifier or SourceIdentifier.SEEK.value).strip().lower()


def unsupported_source_message(source_identifier: str) -> str:
    return (
        f"Source '{source_identifier}' is not supported for collection in this milestone. "
        "Only source 'seek' is enabled."
    )


def get_source_registration(source_identifier: str | None) -> CollectorRegistration:
    source = normalize_source_identifier(source_identifier)
    registration = COLLECTOR_REGISTRY.get(source)
    if registration is None:
        raise UnsupportedSourceError(unsupported_source_message(source))
    return registration


def get_enabled_source_options() -> list[CollectorRegistration]:
    return [
        registration
        for registration in COLLECTOR_REGISTRY.values()
        if registration.enabled and registration.supported
    ]


def get_collector(
    source_identifier: str | None,
    settings: Settings,
    session_factory: type[Session],
) -> Collector:
    registration = get_source_registration(source_identifier)
    return registration.build_collector(settings, session_factory)
