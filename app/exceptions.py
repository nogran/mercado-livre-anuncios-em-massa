"""Domain exceptions for workbook validation and Mercado Libre marketplace operations."""


class WorkbookError(Exception):
    """Base exception for all workbook manipulation errors."""
    pass


class ValidationError(WorkbookError):
    """Raised when workbook structure, rules, or listing payload are invalid."""
    pass


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
