"""crawlutron_extract: stdlib-only content extraction, bounded and deterministic."""

from __future__ import annotations

from crawlutron_extract._extract import DEFAULT_LIMITS, extract
from crawlutron_extract.css_select import SelectorError, validate_css
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.models import Extraction, Quality
from crawlutron_extract.schema import FieldKind, FieldSpec, Schema, extract_records
from crawlutron_extract.xpath_select import validate_xpath

__all__ = [
    "DEFAULT_LIMITS",
    "ExtractLimits",
    "Extraction",
    "FieldKind",
    "FieldSpec",
    "Quality",
    "Schema",
    "extract",
    "extract_records",
    "SelectorError",
    "validate_css",
    "validate_xpath",
]
