"""Decoupled FastAPI server for populating Mercado Libre bulk listing workbooks."""
import io
import json
from typing import List, Optional
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .build import MAX_IMAGE, Image, PhotoUploadFailed, build
from .exceptions import ValidationError, MarketplaceCredentialError, MarketplaceUploadError, RequestRejected
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
        content={"detail": str(exc), "code": "VALIDATION_ERROR", "errors": exc.errors}
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


@app.exception_handler(RequestRejected)
async def handle_rejected_request(request, exc: RequestRejected):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), "code": exc.code})


@app.exception_handler(PhotoUploadFailed)
async def handle_partial_upload(request, exc: PhotoUploadFailed):
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"detail": str(exc), "code": exc.code, "uploaded": exc.uploaded}
    )


@app.get("/")
def root():
    """Service health and metadata info."""
    return {
        "project": "mercado-livre-anuncios-em-massa",
        "version": "0.1.0",
        "status": "online",
        "docs": "/docs",
        "profile_source": "uploaded workbook",
        "verified_template": "Camisetas e Regatas"
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

    wb = OfficialWorkbook(contents, filename=template.filename or '')
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

    wb = OfficialWorkbook(contents, filename=template.filename or '')
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


@app.post(
    "/v1/listings/build",
    summary="Template + rows + images → filled workbook in one call",
    description=(
        "Receives the empty official .xlsx template, the listing rows and the image files. "
        "Each row may name its images in \"photos\" instead of sending column K. "
        "Every row is checked before any upload; then each image is uploaded once, column K "
        "gets the HTTPS URLs in the listed order, and the filled .xlsx is returned."
    ),
    responses={
        200: {"content": {XLSX_MIME: {}}, "description": "Filled workbook, ready for manual upload."},
        400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 413: {"model": ErrorResponse},
        415: {"model": ErrorResponse}, 422: {"model": ErrorResponse}, 502: {"model": ErrorResponse},
    }
)
async def build_listings(
    template: UploadFile = File(..., description="Empty official template file (.xlsx) downloaded from Mercado Libre"),
    payload: str = Form(
        ...,
        description='JSON rows, e.g. {"rows": [{"A": "Camiseta Preta Algodao", "D": "Preto", "G": "M", '
                    '"O": 49.90, "N": 10, "photos": ["frente.jpg", "costas.jpg"]}]}'
    ),
    images: List[UploadFile] = File(default=[], description="Image files named in the rows' photos (PNG, JPEG or WEBP)"),
    authorization: Optional[str] = Header(None, description="'Bearer <token>'; required only when a row has photos")
):
    try:
        data = json.loads(payload)
        rows = data if isinstance(data, list) else data.get("rows", [])
    except (json.JSONDecodeError, AttributeError):
        raise HTTPException(
            status_code=400,
            detail="The payload field must be a valid JSON string containing an array of row objects."
        )
    if not isinstance(rows, list) or not rows:
        raise HTTPException(status_code=400, detail="No listing rows provided for population.")

    contents = await template.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded template file is empty.")

    files = []
    for image in images:
        raw = await image.read(MAX_IMAGE + 1)
        files.append(Image(image.filename or '', raw, image.content_type or ''))

    filled, uploaded = build(contents, template.filename or '', rows, files, authorization)
    return StreamingResponse(
        io.BytesIO(filled),
        media_type=XLSX_MIME,
        headers={
            "Content-Disposition": 'attachment; filename="mercado-libre-bulk-listings-filled.xlsx"',
            "X-Uploaded-Photos": str(uploaded),
        }
    )
