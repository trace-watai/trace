"""Attribution / judge: explains where and why a verified failure happened.

Owned primarily by Darrel Wihandi (attribution/judge logic). See
``docs/modules.md`` and ``docs/attribution_methodology.md``.
"""

from trace_harness.attribution.validation import (
    AttributionValidationIssue,
    validate_attribution_result,
)

__all__ = ["AttributionValidationIssue", "validate_attribution_result"]
