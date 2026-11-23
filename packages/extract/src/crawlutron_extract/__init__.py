"""crawlutron_extract: bounded, deterministic extraction; declares lxml, pdfminer and safe-url."""

from __future__ import annotations

from crawlutron_extract._extract import DEFAULT_LIMITS, extract
from crawlutron_extract.css_select import SelectorError, validate_css
from crawlutron_extract.html_diagnostics import html_diagnostics
from crawlutron_extract.js_endpoints import extract_js_endpoints
from crawlutron_extract.limits import ExtractLimits
from crawlutron_extract.links import LinkRecord, extract_discovered_links
from crawlutron_extract.models import Extraction, Quality
from crawlutron_extract.pagination import detect_pagination
from crawlutron_extract.schema import FieldKind, FieldSpec, Schema, extract_records
from crawlutron_extract.sitemap import extract_sitemap_locs
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
    "extract_discovered_links",
    "extract_js_endpoints",
    "extract_records",
    "extract_sitemap_locs",
    "html_diagnostics",
    "LinkRecord",
    "SelectorError",
    "detect_pagination",
    "validate_css",
    "validate_xpath",
]
