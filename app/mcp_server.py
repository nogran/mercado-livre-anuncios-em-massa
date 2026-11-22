"""MCP server: the same workbook core as the FastAPI server, over stdio.

Files travel by path inside the work folder (``MLAM_WORKDIR``), never as
base64. The Mercado Libre token comes only from ``ML_ACCESS_TOKEN`` in the
server configuration; no tool takes it and no answer repeats it.

Run: ``mercado-livre-anuncios-mcp`` or ``python -m app.mcp_server``.
"""
import json
import os
from typing import Any, Callable, Dict, List, Optional

from typing_extensions import TypedDict

from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, TextContent, ToolAnnotations

from . import __version__
from .build import PHOTOS, PHOTO_COLUMN, Image, PhotoUploadFailed, build, plan_rows
from .exceptions import MarketplaceError, RequestRejected, ValidationError, WorkbookError
from .photos import upload_picture
from .profile import column_number
from .workbook import OfficialWorkbook, digest
from .workdir import FileRefused, WorkDir

TOKEN_VARIABLE = 'ML_ACCESS_TOKEN'
SUMMARY_OPTIONS = 50

server = MCPServer(
    name='mercado-livre-anuncios-em-massa',
    version=__version__,
    instructions=(
        'Fills the official Mercado Libre bulk listing workbook (.xlsx). Start with inspect_template on the '
        'empty workbook the seller downloaded, build one row per color and size with column letters as keys, '
        'check them with validate_rows, then write the file with fill_workbook (photo URLs already in K) or '
        'build_listings (local photos in "photos"). Read mlam://exemplos/camiseta for a complete example. '
        'Paths are relative to the work folder. The seller uploads the finished .xlsx in Mercado Libre.'
    ),
)


class RowError(TypedDict):
    row: Optional[int]
    column: Optional[str]
    message: str


class Field(TypedDict):
    column: str
    name: str
    required: bool
    help: str


class TemplateSummary(TypedDict):
    sha256: str
    filename: str
    sheet: str
    expires: Optional[str]
    rows_per_batch: int
    fields: List[Field]
    required: List[str]
    strict: List[str]
    numeric_columns: List[str]
    text_limits: Dict[str, int]
    number_rules: Dict[str, Dict[str, Any]]
    default_fields: Dict[str, str]
    options: Dict[str, List[str]]
    omitted_options: Dict[str, int]
    guides: List[Dict[str, Any]]
    guide_columns: List[Dict[str, Any]]


class Validation(TypedDict):
    ok: bool
    errors: List[RowError]


class ToolFailure(Exception):
    """A refusal the agent can act on: code, message and optional details."""

    def __init__(self, code: str, detail: str, **extra: Any):
        super().__init__(detail)
        self.code = code
        self.extra = extra


def _token() -> str:
    return (os.environ.get(TOKEN_VARIABLE) or '').strip()


def _scrub(text: str) -> str:
    """Never let the configured token reach an answer, even inside a remote message."""
    token = _token()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    return text.replace(token, '***') if len(token) >= 4 else text


def _failure(error: Exception) -> ToolFailure:
    if isinstance(error, ToolFailure):
        return error
    if isinstance(error, FileRefused):
        return ToolFailure(error.code, str(error))
    if isinstance(error, ValidationError):
        return ToolFailure('VALIDATION_ERROR', str(error), errors=error.errors)
    if isinstance(error, PhotoUploadFailed):
        return ToolFailure('PHOTO_UPLOAD_FAILED', str(error), uploaded=error.uploaded, cause=error.code)
    if isinstance(error, MarketplaceError):
        return ToolFailure(error.code, str(error))
    if isinstance(error, RequestRejected):
        return ToolFailure(error.code, str(error))
    if isinstance(error, WorkbookError):
        return ToolFailure('WORKBOOK_ERROR', str(error))
    raise error


def _answer(work: Callable[[], Dict[str, Any]]) -> Any:
    """Run a tool body; a known refusal becomes an error result with structured content."""
    try:
        return work()
    except (ToolFailure, FileRefused, ValidationError, WorkbookError, MarketplaceError, RequestRejected) as error:
        failure = _failure(error)
        body = {'code': failure.code, 'detail': _scrub(str(failure)), **failure.extra}
        text = json.dumps(body, ensure_ascii=False)
        return CallToolResult(content=[TextContent(type='text', text=_scrub(text))],
                              structured_content=json.loads(_scrub(text)), is_error=True)


def _workbook(work: WorkDir, template_path: str) -> OfficialWorkbook:
    real = work.resolve(template_path)
    return OfficialWorkbook(work.read_template(template_path), filename=real.name)


def _photo_paths(rows: List[Any]) -> List[str]:
    """Every photo path named by the rows, first use first, without repeats."""
    order: List[str] = []
    for row in rows:
        names = row.get(PHOTOS) if isinstance(row, dict) else None
        if isinstance(names, list):
            order.extend(n for n in names if isinstance(n, str) and n.strip() and n not in order)
    return order


def _check_rows(work: WorkDir, wb: OfficialWorkbook, rows: List[Any], read: bool) -> List[Image]:
    """Check every row and every photo file. Raise ValidationError with all problems.

    A photo that cannot be used becomes a column K error on each row that
    names it; the row rules come from ``plan_rows`` as in the HTTP server.
    """
    images: List[Image] = []
    refused: Dict[str, str] = {}
    for path in _photo_paths(rows):
        try:
            mime = work.image_mime(path)
            raw = work.read_image(path) if read else b''
            if not read:
                work.check_image(path)
            images.append(Image(path, raw, mime))
        except FileRefused as error:
            refused[path] = str(error)

    file_errors: Dict[int, str] = {}
    for index, row in enumerate(rows, start=1):
        names = row.get(PHOTOS) if isinstance(row, dict) else None
        bad = [n for n in names if n in refused] if isinstance(names, list) else []
        if bad:
            file_errors[index] = f'Listing row {index}, column {PHOTO_COLUMN}: {refused[bad[0]]}'

    errors: List[Dict[str, Any]] = []
    try:
        plan_rows(wb, rows, {image.name: image for image in images})
    except ValidationError as error:
        if not error.errors:
            raise
        errors = [e for e in error.errors if not (e['column'] == PHOTO_COLUMN and e['row'] in file_errors)]
    errors.extend({'row': row, 'column': PHOTO_COLUMN, 'message': message} for row, message in file_errors.items())
    if errors:
        order = {column: position for position, column in enumerate(wb.profile.editable)}
        errors.sort(key=lambda item: (item['row'], order.get(item['column'], -1)))
        raise ValidationError(errors[0]['message'], errors=errors)
    return images


@server.tool(
    title='Inspect the empty workbook',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def inspect_template(template_path: str, columns: Optional[List[str]] = None) -> TemplateSummary:
    """Read the empty official workbook: fields, required columns, allowed options, limits, size guides, expiry.

    Without ``columns`` the answer lists options only for strict columns with up to 50 items;
    ``omitted_options`` gives the size of the others. Pass ``columns`` (e.g. ["D", "G"]) to get
    every option of those columns.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        meta = _workbook(work, template_path).metadata()
        options: Dict[str, List[str]] = meta['options']
        if columns:
            wanted = [c.strip().upper() for c in columns]
            unknown = [c for c in wanted if c not in {f['coluna'] for f in meta['fields']}]
            if unknown:
                raise ToolFailure('UNKNOWN_COLUMN', f'Columns not in this template: {", ".join(unknown)}.')
            shown = {c: options.get(c, []) for c in wanted}
            omitted = {}
        else:
            strict = set(meta['strict'])
            shown = {c: v for c, v in options.items() if c in strict and len(v) <= SUMMARY_OPTIONS}
            omitted = {c: len(v) for c, v in options.items() if c not in shown}
        return {
            'sha256': meta['sha256'],
            'filename': meta['filename'],
            'sheet': meta['sheet'],
            'expires': meta['expires'],
            'rows_per_batch': meta['last_row'] - meta['first_row'] + 1,
            'fields': [
                {'column': f['coluna'], 'name': f['campo'], 'required': f['obrigatorio'], 'help': f['instrucao']}
                for f in sorted(meta['fields'], key=lambda f: column_number(f['coluna']))
            ],
            'required': [f['coluna'] for f in meta['fields'] if f['obrigatorio']],
            'strict': meta['strict'],
            'numeric_columns': meta['numeric_columns'],
            'text_limits': meta['text_limits'],
            'number_rules': meta['number_rules'],
            'default_fields': meta['default_fields'],
            'options': shown,
            'omitted_options': omitted,
            'guides': meta['guides'],
            'guide_columns': meta['guide_columns'],
        }
    return _answer(run)


@server.tool(
    title='Check listing rows',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
)
def validate_rows(template_path: str, rows: List[Dict[str, Any]]) -> Validation:
    """Check rows against the workbook without writing or uploading anything.

    Each row is an object keyed by column letter ("A", "D", "G", ...). A row may list local
    photo paths in "photos" instead of URLs in "K"; each file is checked (inside the work
    folder, PNG/JPEG/WEBP, up to 10 MB) but not uploaded. Returns ``ok`` and every problem
    as ``{row, column, message}``.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        wb = _workbook(work, template_path)
        try:
            _check_rows(work, wb, rows, read=False)
        except ValidationError as error:
            return {'ok': False, 'errors': error.errors or [{'row': None, 'column': None, 'message': str(error)}]}
        return {'ok': True, 'errors': []}
    return _answer(run)


class Written(TypedDict):
    output_path: str
    rows: int
    sha256: str


@server.tool(
    title='Write the filled workbook',
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=False),
)
def fill_workbook(template_path: str, rows: List[Dict[str, Any]], output_path: str,
                  overwrite: bool = False) -> Written:
    """Write the rows into a copy of the empty workbook and save it as ``output_path`` (.xlsx).

    Photos must already be HTTPS URLs in column "K" (comma separated); for local photo files use
    build_listings. Nothing is written when any row is invalid; the error lists every problem.
    An existing ``output_path`` is replaced only with ``overwrite: true``.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        target = work.output(output_path, overwrite)
        if any(isinstance(row, dict) and PHOTOS in row for row in rows):
            raise ToolFailure('PHOTOS_NEED_BUILD', 'Rows with "photos" go to build_listings, which uploads them.')
        filled = _workbook(work, template_path).export(rows)
        work.write(target, filled)
        return {'output_path': work.relative(target), 'rows': len(rows), 'sha256': digest(filled)}
    return _answer(run)


TOKEN_MISSING = (f'Set {TOKEN_VARIABLE} in the MCP server configuration with a Mercado Libre access token '
                 'to upload photos.')


class Photo(TypedDict):
    picture_id: Optional[str]
    url: str
    obtained_at: str
    expires_at: str


@server.tool(
    title='Upload one photo to Mercado Libre',
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=True),
)
def upload_photo(image_path: str) -> Photo:
    """Upload one local photo (PNG, JPEG or WEBP, up to 10 MB) and return its HTTPS URL for column "K".

    Uses the token in ML_ACCESS_TOKEN. Mercado Libre keeps a picture that no listing uses for a
    limited time, so upload close to sending the workbook.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        mime = work.image_mime(image_path)
        raw = work.read_image(image_path)
        if not _token():
            raise ToolFailure('ML_TOKEN_MISSING', TOKEN_MISSING)
        return upload_picture(_token(), raw, mime)
    return _answer(run)


class Built(TypedDict):
    output_path: str
    rows: int
    uploaded: int
    sha256: str


@server.tool(
    title='Upload photos and write the workbook',
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False,
                                open_world_hint=True),
)
def build_listings(template_path: str, rows: List[Dict[str, Any]], output_path: str,
                   overwrite: bool = False) -> Built:
    """Upload the rows' local photos and write the filled workbook as ``output_path`` (.xlsx).

    Each row lists photo paths in "photos" (in listing order) instead of URLs in "K"; a photo used
    by several rows is uploaded once. Every row and every file is checked before the first upload.
    If an upload fails midway, nothing is written and ``uploaded`` says how many went up.
    Uses the token in ML_ACCESS_TOKEN.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        target = work.output(output_path, overwrite)
        real = work.resolve(template_path)
        template = work.read_template(template_path)
        wb = OfficialWorkbook(template, filename=real.name)
        images = _check_rows(work, wb, rows, read=True)
        if images and not _token():
            raise ToolFailure('ML_TOKEN_MISSING', TOKEN_MISSING)
        filled, uploaded = build(template, real.name, rows, images, _token())
        work.write(target, filled)
        return {'output_path': work.relative(target), 'rows': len(rows), 'uploaded': uploaded,
                'sha256': digest(filled)}
    return _answer(run)


def main() -> None:
    WorkDir()
    server.run('stdio')


if __name__ == '__main__':
    main()
