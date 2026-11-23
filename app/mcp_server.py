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
from . import items as listings
from .exceptions import MarketplaceError, RequestRejected, ValidationError, WorkbookError
from .photos import upload_picture
from .profile import column_number
from .workbook import OfficialWorkbook, digest
from .workdir import FileRefused, WorkDir

TOKEN_VARIABLE = 'ML_ACCESS_TOKEN'
SUMMARY_OPTIONS = 50
MAX_ROWS_FILE = 16 * 1024 * 1024

server = MCPServer(
    name='mercado-livre-anuncios-em-massa',
    version=__version__,
    instructions=(
        'Fills the official Mercado Libre bulk listing workbook (.xlsx). Start with inspect_template on the '
        'empty workbook the seller downloaded, build one row per color and size with column letters as keys, '
        'check them with validate_rows, then write the file with fill_workbook (photo URLs already in K) or '
        'build_listings (local photos in "photos"). Many rows? Save them as a JSON file in the work folder and '
        'pass rows_path instead of rows. Read mlam://exemplos/camiseta for a complete example. '
        'Paths are relative to the work folder. The seller uploads the finished .xlsx in Mercado Libre. '
        'After Mercado Libre creates the listings: find_listings (same titles or rows file), then '
        'set_free_shipping and fill_attributes (preview first; apply: true only after the seller agrees) and '
        'video_upload_links. listing_stats reads visits and sales per item and per family.'
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


def _rows(work: WorkDir, rows: Optional[List[Any]], rows_path: Optional[str]) -> List[Any]:
    """The rows given inline or read from a JSON file in the work folder; exactly one of the two.

    The file holds a list of rows or ``{"rows": [...]}``, as in the examples.
    """
    if rows is not None and rows_path:
        raise ToolFailure('ROWS_AMBIGUOUS', 'Pass rows or rows_path, not both.')
    if not rows_path:
        if rows is None:
            raise ToolFailure('ROWS_MISSING', 'Pass rows (a list of row objects) or rows_path (a JSON file).')
        return rows
    raw = work.read(rows_path, MAX_ROWS_FILE, 'Rows file')
    try:
        data = json.loads(raw.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as error:
        raise ToolFailure('ROWS_FILE_INVALID', f'Rows file "{rows_path}" is not valid UTF-8 JSON: {error}.')
    if isinstance(data, dict):
        data = data.get('rows')
    if not isinstance(data, list):
        raise ToolFailure('ROWS_FILE_INVALID',
                          f'Rows file "{rows_path}" must hold a list of rows or an object with a "rows" list.')
    return data


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
def validate_rows(template_path: str, rows: Optional[List[Dict[str, Any]]] = None,
                  rows_path: Optional[str] = None) -> Validation:
    """Check rows against the workbook without writing or uploading anything.

    Each row is an object keyed by column letter ("A", "D", "G", ...). A row may list local
    photo paths in "photos" instead of URLs in "K"; each file is checked (inside the work
    folder, PNG/JPEG/WEBP, up to 10 MB) but not uploaded. Returns ``ok`` and every problem
    as ``{row, column, message}``.

    Pass the rows inline in ``rows`` or, for many rows, as a JSON file in the work folder in
    ``rows_path`` (a list of rows or ``{"rows": [...]}``); exactly one of the two.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        given = _rows(work, rows, rows_path)
        wb = _workbook(work, template_path)
        try:
            _check_rows(work, wb, given, read=False)
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
def fill_workbook(template_path: str, output_path: str, rows: Optional[List[Dict[str, Any]]] = None,
                  rows_path: Optional[str] = None, overwrite: bool = False) -> Written:
    """Write the rows into a copy of the empty workbook and save it as ``output_path`` (.xlsx).

    Photos must already be HTTPS URLs in column "K" (comma separated); for local photo files use
    build_listings. Nothing is written when any row is invalid; the error lists every problem.
    An existing ``output_path`` is replaced only with ``overwrite: true``.

    Pass the rows inline in ``rows`` or, for many rows, as a JSON file in the work folder in
    ``rows_path`` (a list of rows or ``{"rows": [...]}``); exactly one of the two.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        given = _rows(work, rows, rows_path)
        target = work.output(output_path, overwrite)
        if any(isinstance(row, dict) and PHOTOS in row for row in given):
            raise ToolFailure('PHOTOS_NEED_BUILD', 'Rows with "photos" go to build_listings, which uploads them.')
        filled = _workbook(work, template_path).export(given)
        work.write(target, filled)
        return {'output_path': work.relative(target), 'rows': len(given), 'sha256': digest(filled)}
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
def build_listings(template_path: str, output_path: str, rows: Optional[List[Dict[str, Any]]] = None,
                   rows_path: Optional[str] = None, overwrite: bool = False) -> Built:
    """Upload the rows' local photos and write the filled workbook as ``output_path`` (.xlsx).

    Each row lists photo paths in "photos" (in listing order) instead of URLs in "K"; a photo used
    by several rows is uploaded once. Every row and every file is checked before the first upload.
    If an upload fails midway, nothing is written and ``uploaded`` says how many went up.
    Uses the token in ML_ACCESS_TOKEN.

    Pass the rows inline in ``rows`` or, for many rows, as a JSON file in the work folder in
    ``rows_path`` (a list of rows or ``{"rows": [...]}``); exactly one of the two.
    """
    def run() -> Dict[str, Any]:
        work = WorkDir()
        given = _rows(work, rows, rows_path)
        target = work.output(output_path, overwrite)
        real = work.resolve(template_path)
        template = work.read_template(template_path)
        wb = OfficialWorkbook(template, filename=real.name)
        images = _check_rows(work, wb, given, read=True)
        if images and not _token():
            raise ToolFailure('ML_TOKEN_MISSING', TOKEN_MISSING)
        filled, uploaded = build(template, real.name, given, images, _token())
        work.write(target, filled)
        return {'output_path': work.relative(target), 'rows': len(given), 'uploaded': uploaded,
                'sha256': digest(filled)}
    return _answer(run)


def _seller() -> listings.MercadoLibre:
    if not _token():
        raise ToolFailure('ML_TOKEN_MISSING', f'Set {TOKEN_VARIABLE} in the MCP server configuration with a '
                                              'Mercado Libre access token to read and change listings.')
    return listings.MercadoLibre(_token())


class ItemFailure(TypedDict):
    id: str
    error: str


class FoundItem(TypedDict):
    id: str
    family_name: str
    status: Optional[str]


class Found(TypedDict):
    items: List[FoundItem]
    item_ids: List[str]
    per_title: Dict[str, int]
    missing_titles: List[str]
    expected_items: Optional[int]
    count_matches: Optional[bool]
    searched: List[str]
    scanned: int
    failed: List[ItemFailure]


@server.tool(
    title='Find the listings a workbook created',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def find_listings(titles: Optional[List[str]] = None, rows_path: Optional[str] = None,
                  search_terms: Optional[List[str]] = None) -> Found:
    """Find the seller's items whose family title is one of the workbook titles (column "A").

    Pass ``titles`` or the same ``rows_path`` used to build the workbook; with a rows file,
    ``expected_items`` is its number of rows and ``count_matches`` says whether every row became
    an item (Mercado Libre may still be creating them: wait a few minutes and repeat). Matching
    ignores case. ``search_terms`` narrow the account search (default: the first two words of
    each title). Read only. Uses the token in ML_ACCESS_TOKEN.
    """
    def run() -> Dict[str, Any]:
        expected = None
        wanted = list(titles or [])
        if rows_path:
            if titles:
                raise ToolFailure('ROWS_AMBIGUOUS', 'Pass titles or rows_path, not both.')
            given = _rows(WorkDir(), None, rows_path)
            wanted = [row.get('A') for row in given if isinstance(row, dict) and isinstance(row.get('A'), str)]
            expected = len(given)
        found = listings.find_items(_seller(), wanted, search_terms)
        found['expected_items'] = expected
        found['count_matches'] = None if expected is None else len(found['item_ids']) == expected
        return found
    return _answer(run)


class Changes(TypedDict):
    applied: bool
    total: int
    changed: List[Any]
    would_change: List[Any]
    already: List[str]
    failed: List[ItemFailure]
    counts: Dict[str, int]


@server.tool(
    title='Turn on free shipping',
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=True),
)
def set_free_shipping(item_ids: List[str], apply: bool = False) -> Changes:
    """Turn on free shipping on the items that do not have it (Mercado Libre publishes the workbook without it).

    Without ``apply`` nothing changes: the answer lists ``would_change`` and ``already``. Show it to
    the seller and repeat with ``apply: true`` only after they agree; each change is read back to
    confirm. Never touches price, stock or title. Uses the token in ML_ACCESS_TOKEN.
    """
    return _answer(lambda: listings.free_shipping(_seller(), listings.clean_ids(item_ids), apply))


@server.tool(
    title='Fill empty listing attributes',
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                                open_world_hint=True),
)
def fill_attributes(item_ids: List[str], attributes: Dict[str, str], apply: bool = False) -> Changes:
    """Fill item attributes (technical sheet) only where they are empty; a filled value is never replaced.

    ``attributes`` maps Mercado Libre attribute ids to values, e.g. {"COMPOSITION": "Algodão",
    "SLEEVE_TYPE": "Curta"}. Without ``apply`` nothing changes: ``would_change`` lists, per item,
    the attributes that would be written. Repeat with ``apply: true`` only after the seller agrees;
    each change is read back to confirm. Uses the token in ML_ACCESS_TOKEN.
    """
    return _answer(lambda: listings.fill_attributes(_seller(), listings.clean_ids(item_ids), attributes, apply))


class VideoLink(TypedDict):
    family_name: str
    item_id: str
    url: str


class VideoLinks(TypedDict):
    links: List[VideoLink]
    failed: List[ItemFailure]


@server.tool(
    title='Video upload links',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def video_upload_links(item_ids: List[str]) -> VideoLinks:
    """One Mercado Libre video upload link per listing family (all colors and sizes of one title).

    The seller opens each link signed in to Mercado Libre to send the video. Read only. Uses the
    token in ML_ACCESS_TOKEN.
    """
    return _answer(lambda: listings.video_links(_seller(), listings.clean_ids(item_ids)))


class ItemStats(TypedDict):
    id: str
    family_name: str
    status: Optional[str]
    sold: int
    visits: int
    visits_7d: int
    age_days: Optional[int]


class FamilyStats(TypedDict):
    family_name: str
    items: int
    active: int
    sold: int
    visits: int
    visits_7d: int
    age_days: Optional[int]


class Stats(TypedDict):
    days: int
    items: List[ItemStats]
    families: List[FamilyStats]
    failed: List[ItemFailure]


@server.tool(
    title='Visits and sales per listing',
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
def listing_stats(item_ids: List[str], days: int = 30) -> Stats:
    """Visits (last ``days``, 1 to 90, and last 7), sales, status and age per item, summed per family.

    ``families`` comes sorted by visits, most first. ``sold`` is the item's total since it was
    created, not only in the window. Read only; nothing changes. Uses the token in ML_ACCESS_TOKEN.
    """
    return _answer(lambda: listings.stats(_seller(), listings.clean_ids(item_ids), days))


COLUMNS = """Camisetas e Regatas: one row per color and size. Keys are column letters.

| Column | Field | Example |
|---|---|---|
| A | Title (up to 60 characters, same for every variation) | Camiseta Infantil Algodao Dinossauro Preta |
| C | Condition | Novo |
| D | Color | Preto |
| G | Size | 2 |
| H | Brand | Genérica |
| I | Gender | Sem gênero infantil |
| J | Size guide code | 1000001 |
| K | Photo URLs (HTTPS, comma separated), or "photos" with local paths in build_listings | |
| L | GTIN, or a reason from the list | O produto não tem código cadastrado |
| N | Stock | 10 |
| O | Price (8 to 1000000, up to two decimals) | 39.90 |
| S, U, V, W | Listing type, shipping, shipping cost, pickup | Clássico, Mercado Envios, ... |
| AA-AD | Model, garment type, sleeve, material | Dinossauro, Camiseta, Curta, Algodão |

Allowed options and required columns come from inspect_template on the seller's own workbook.
"""


def _example() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for folder in (os.path.join(here, 'examples'), os.path.join(os.path.dirname(here), 'examples')):
        path = os.path.join(folder, 'build-camiseta.json')
        if os.path.isfile(path):
            with open(path, encoding='utf8') as handle:
                return handle.read()
    raise FileNotFoundError('build-camiseta.json is not installed with the package.')


@server.resource('mlam://exemplos/camiseta', name='camiseta-example', mime_type='text/markdown',
                 description='Column guide and a complete build_listings example for Camisetas e Regatas.')
def camiseta_example() -> str:
    return f'{COLUMNS}\nExample rows for build_listings (photos are paths in the work folder):\n\n```json\n{_example()}```\n'


def main() -> None:
    WorkDir()
    server.run('stdio')


if __name__ == '__main__':
    main()
