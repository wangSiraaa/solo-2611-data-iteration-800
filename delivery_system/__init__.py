"""Source-segment catalog + multilingual delivery repository.

Two deliberately separate sides:

* :class:`delivery_system.source_catalog.SourceCatalog` owns documents,
  stable segment identities, their order, current revisions and text.  It
  exposes an immutable :class:`SourceProduct` snapshot.
* :class:`delivery_system.terminology.TerminologyCatalog` owns publishable
  terminology books: concepts, a chapter tree, per-chapter spelling bindings,
  segment ownership and hand-indexed markers, frozen into independent
  versions.  The delivery side reads them only through
  :class:`delivery_system.terminology.PublishedTerminologyReader`.
* :class:`delivery_system.delivery_repository.DeliveryRepository` owns
  translation entries, published packages, regional quotes/locks and frozen
  regional editions.  It reads the *published source product* and the
  *published terminology versions* read-only, and never treats a position
  number as identity.

No translation, word-sense recognition or sentence generation happens here:
text is opaque and every draft is supplied by a human editor.
"""

from .coverage import solve_cover
from .delivery_repository import DeliveryRepository
from .models import (
    CoverChunk,
    CoverDiagnosis,
    CoverResult,
    SourceProduct,
    SourceRef,
    SourceSegment,
    TranslationAssessment,
    TranslationEntry,
)
from .regional import RegionalPlan, RegionalQuote, solve_regional
from .source_catalog import SourceCatalog
from .terminology import (
    PublishedTerminologyReader,
    TerminologyCatalog,
    TerminologyVersion,
)

__all__ = [
    "CoverChunk",
    "CoverDiagnosis",
    "CoverResult",
    "DeliveryRepository",
    "PublishedTerminologyReader",
    "RegionalPlan",
    "RegionalQuote",
    "SourceCatalog",
    "SourceProduct",
    "SourceRef",
    "SourceSegment",
    "TerminologyCatalog",
    "TerminologyVersion",
    "TranslationAssessment",
    "TranslationEntry",
    "solve_cover",
    "solve_regional",
]
