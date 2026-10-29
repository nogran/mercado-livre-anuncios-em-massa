"""Decoupled FastAPI server for populating Mercado Libre bulk listing workbooks."""
import io
import json
from typing import Optional
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .exceptions import ValidationError, MarketplaceCredentialError, MarketplaceUploadError
from .workbook import OfficialWorkbook
from .photos import upload_picture
from .schemas import InspectResponse, PhotoUploadResponse, ErrorResponse

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

app = FastAPI(
    title="Mercado Libre - Bulk Listings Excel Generator",
    description="Decoupled, open-source API to populate and validate official Mercado Libre bulk listing spreadsheets.",
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValidationError)
async def handle_validation_error(request, exc: ValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": str(exc), "code": "VALIDATION_ERROR"}
    )


@app.exception_handler(MarketplaceCredentialError)
async def handle_credential_error(request, exc: MarketplaceCredentialError):
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc), "code": exc.code}
    )


@app.exception_handler(MarketplaceUploadError)
async def handle_upload_error(request, exc: MarketplaceUploadError):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": str(exc), "code": exc.code}
    )


@app.get("/")
def root():
    """Service health and metadata info."""
    return {
        "project": "mercado-livre-anuncios-em-massa",
        "version": "0.1.0",
        "status": "online",
        "docs": "/docs",
        "supported_category": "T-shirts and Tank Tops (Apparel)"
    }


@app.post(
    "/v1/templates/inspect",
    response_model=InspectResponse,
    summary="Inspect official Mercado Libre spreadsheet template",
    description="Receives an empty official .xlsx template and inspects its rules, columns, options, and validity."
)
async def inspect_template(
    template: UploadFile = File(..., description="Official .xlsx template file downloaded from Mercado Libre seller panel")
):
    contents = await template.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    wb = OfficialWorkbook(contents)
    return wb.metadata()


@app.post(
    "/v1/workbooks/fill",
    summary="Populate official workbook with product rows",
    description="Receives an empty official .xlsx template and listing rows payload. Returns the populated .xlsx file ready for manual upload to Mercado Libre.",
    responses={
        200: {
            "content": {XLSX_MIME: {}},
            "description": "Populated and verified official Excel workbook."
        },
        422: {"model": ErrorResponse}
    }
)
async def fill_workbook(
    template: UploadFile = File(..., description="Empty official template file (.xlsx)"),
    payload: str = Form(
        ...,
        description="JSON payload containing list of listing rows, e.g. [{\"A\": \"T-shirt Title\", \"C\": \"Novo\", \"D\": \"Preto\", \"G\": \"M\", \"O\": 49.90, \"N\": 10}]"
    )
):
    try:
        data = json.loads(payload)
        rows = data if isinstance(data, list) else data.get("rows", [])
    except (json.JSONDecodeError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail="The payload field must be a valid JSON string containing an array of row objects."
        )

    if not rows:
        raise HTTPException(status_code=400, detail="No listing rows provided for population.")

    contents = await template.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded template file is empty.")

    wb = OfficialWorkbook(contents)
    filled_bytes = wb.export(rows)

    filename = "mercado-libre-bulk-listings-filled.xlsx"
    return StreamingResponse(
        io.BytesIO(filled_bytes),
        media_type=XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.post(
    "/v1/photos/upload",
    response_model=PhotoUploadResponse,
    summary="Upload pictures to Mercado Libre pictures API",
    description="Uploads an image file to Mercado Libre and returns the public HTTPS URL for the spreadsheet's photo column (K)."
)
async def upload_photo(
    image: UploadFile = File(..., description="Image file asset (PNG, JPEG, or WEBP)"),
    authorization: Optional[str] = Header(None, description="Mercado Libre seller Access Token formatted as 'Bearer <token>'")
):
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authorization header with a valid Mercado Libre Access Token is required."
        )

    content_type = image.content_type or 'image/jpeg'
    image_bytes = await image.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="Uploaded image file is empty.")

    result = upload_picture(
        token=authorization,
        raw=image_bytes,
        mime=content_type
    )
    return result
