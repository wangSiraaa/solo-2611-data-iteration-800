"""Domain errors with actionable messages."""

from __future__ import annotations

from typing import Optional


class DeliveryError(Exception):
    """Base class for expected domain failures."""


class DocumentExists(DeliveryError):
    pass


class DocumentNotFound(DeliveryError):
    pass


class SegmentNotFound(DeliveryError):
    pass


class DuplicateSegment(DeliveryError):
    pass


class InvalidPosition(DeliveryError):
    pass


class RevisionConflict(DeliveryError):
    pass


class TranslationExists(DeliveryError):
    pass


class InvalidTranslation(DeliveryError):
    pass


class PackageExistsImmutable(DeliveryError):
    """A package id was reused with different frozen content."""


class PackageNotFound(DeliveryError):
    """A package or edition id does not exist."""


class NoCoverError(DeliveryError):
    """Publishing was attempted but no exact current cover exists."""

    def __init__(self, message: str, result: Optional[object] = None) -> None:
        super().__init__(message)
        self.result = result


# ----------------------------------------------------------------------
# terminology book (source side)
# ----------------------------------------------------------------------
class TerminologyError(DeliveryError):
    """Base class for terminology-book failures."""


class ConceptExists(TerminologyError):
    pass


class ConceptNotFound(TerminologyError):
    pass


class ChapterExists(TerminologyError):
    pass


class ChapterNotFound(TerminologyError):
    pass


class ChapterCycle(TerminologyError):
    """Reparenting would create (or a snapshot contains) a chapter cycle."""


class InvalidBinding(TerminologyError):
    pass


class DanglingSegment(TerminologyError):
    """A document segment has no chapter, or an assignment points nowhere."""


class UndefinedMarker(TerminologyError):
    """A marker's spelling has no binding reachable from its chapter."""


class MarkerExists(TerminologyError):
    pass


class MarkerNotFound(TerminologyError):
    pass


class TerminologyVersionNotFound(TerminologyError):
    pass


# ----------------------------------------------------------------------
# regional editions (delivery side)
# ----------------------------------------------------------------------
class RegionalError(DeliveryError):
    """Base class for regional-edition failures."""


class InvalidRegionalQuote(RegionalError):
    pass


class RegionalQuoteExists(RegionalError):
    pass


class InvalidRegionalLock(RegionalError):
    pass


class RegionalProblemTooLarge(RegionalError):
    """More than 12 concepts or more than 3 usages per concept."""


class NoRegionalEdition(RegionalError):
    """No feasible regional edition exists; nothing is written."""

    def __init__(self, message: str, plan: Optional[object] = None) -> None:
        super().__init__(message)
        self.plan = plan
