from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class CollectorInput:
    keywords: str
    location: str
    date_listed: str
    max_pages: int
    run_id: int
    start_page: int = 1


class CollectorError(Exception):
    """Base collector failure."""


class LayoutError(CollectorError):
    """The page did not match the expected structure."""


class AccessChallengeError(CollectorError):
    """The site requested login, CAPTCHA, verification, or similar user action."""


class LoginRequiredError(CollectorError):
    """The site requested a non-optional login before results were available."""


class Collector(ABC):
    @abstractmethod
    def collect(self, collector_input: CollectorInput) -> None:
        """Run collection and persist results incrementally."""
