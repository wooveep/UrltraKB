class PageIndexError(Exception):
    """Base exception for all PageIndex SDK errors."""
    pass


class CollectionNotFoundError(PageIndexError):
    """Collection does not exist."""
    pass


class CollectionAlreadyExistsError(PageIndexError):
    """Collection already exists (create_collection, not get_or_create)."""
    pass


class DocumentNotFoundError(PageIndexError):
    """Document ID not found."""
    pass


class IndexingError(PageIndexError):
    """Indexing pipeline failure."""
    pass


class FileTypeError(PageIndexError, ValueError):
    """Unsupported file type.

    Also subclasses ValueError so pre-SDK ``except ValueError`` around indexing
    (0.2.x raised ValueError for an unsupported file format) still catches it.
    Note: because of this, an ``except ValueError`` clause ahead of an
    ``except FileTypeError`` clause in the same try block will catch it first —
    if you need FileTypeError-specific handling, put that except before (or
    instead of) a bare ValueError one.
    """
    pass


class IndexQualityError(IndexingError):
    """Verified structure quality failure; a bounded rebuild may help."""
