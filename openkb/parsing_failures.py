"""Translate document-format errors without weakening storage or execution checks."""

from xml.etree.ElementTree import ParseError
from xml.sax import SAXParseException
from zipfile import BadZipFile

import pymupdf
from defusedxml.common import DefusedXmlException
from lxml.etree import XMLSyntaxError

from openkb.processing import processing_checkpoint


class DocumentContentError(ValueError):
    """A format reader rejected document bytes, independently of KB storage."""


def read_office_package(operation, stream, **options):
    """Load an already-read in-memory package before any KB callback or write."""
    try:
        return operation(stream, **options)
    except (KeyError, ValueError, OSError) as exc:
        raise DocumentContentError(type(exc).__name__) from exc


def read_document(operation, *args, **kwargs):
    """Retain an unreadable original as an explicit zero-content parsing result.

    Only parser-specific exceptions belong here. In particular, an arbitrary
    ValueError, OSError or ProcessingIncomplete may mean damaged stored evidence,
    a failed transaction, cancellation or an exhausted execution budget.
    """
    try:
        return operation(*args, **kwargs)
    except (
        BadZipFile,
        ParseError,
        SAXParseException,
        DefusedXmlException,
        XMLSyntaxError,
        pymupdf.FileDataError,
        DocumentContentError,
    ) as exc:
        processing_checkpoint("parsing")
        return [], [
            {"status": "needs_review", "reason": "source_content_unparsed:" + type(exc).__name__}
        ]
