"""Domain exceptions for workbook validation and Mercado Libre marketplace operations."""
from typing import Any, Dict, List, Optional


class WorkbookError(Exception):
    """Base exception for all workbook manipulation errors."""
    pass


class ValidationError(WorkbookError):
    """Raised when workbook structure, rules, or listing payload are invalid.

    ``errors`` lists every row problem found, as ``{"row", "column", "message"}``.
    The exception text is the first message, so callers that read only the
    text keep working.
    """
    def __init__(self, message: str, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.errors = list(errors or [])


class MarketplaceError(Exception):
    """Base exception for Mercado Libre remote API communications."""
    def __init__(self, message: str, code: str = "ML_ERROR"):
        super().__init__(message)
        self.code = code


class MarketplaceCredentialError(MarketplaceError):
    """Raised when the access token is invalid, expired, or rejected."""
    pass


class MarketplaceUploadError(MarketplaceError):
    """Raised when uploading pictures or files to Mercado Libre fails."""
    pass


class RequestRejected(Exception):
    """Raised before any upload when the request itself cannot be served.

    ``status`` is the HTTP status the server answers with (400, 401, 413, 415).
    """
    def __init__(self, message: str, status: int, code: str):
        super().__init__(message)
        self.status = status
        self.code = code
