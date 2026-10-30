"""Pydantic schemas for API requests, responses, and validation models."""
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field


class ColumnField(BaseModel):
    coluna: str
    campo: str
    obrigatorio: bool
    instrucao: Optional[str] = None


class InspectResponse(BaseModel):
    sha256: str
    sheet: str
    filename: str
    expires: str
    first_row: int
    last_row: int
    numeric_columns: List[str]
    default_fields: Dict[str, str]
    options: Dict[str, List[str]]
    strict: List[str]
    guides: List[Dict[str, Any]]
    guide_columns: List[Dict[str, str]]
    fields: List[ColumnField]


class FillPayload(BaseModel):
    rows: List[Dict[str, Any]] = Field(
        ...,
        description="List of product rows to populate. Each row is a dictionary of {column_letter: value}, e.g. {'A': 'Title', 'D': 'Black', 'G': 'M'}."
    )


class PhotoUploadResponse(BaseModel):
    picture_id: Optional[str]
    url: str
    obtained_at: str
    expires_at: str


class ErrorResponse(BaseModel):
    detail: str
    code: Optional[str] = None
