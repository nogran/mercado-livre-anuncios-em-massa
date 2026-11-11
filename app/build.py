"""One call: official template + listing rows + images → filled workbook.

Order: read the template, check every row and every photo reference, then
upload each image once, write column K and fill the workbook. Nothing is
uploaded while any row or photo reference is wrong, so a refused request
leaves no picture behind.
"""
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import MarketplaceError, RequestRejected, ValidationError
from .photos import MIME_EXTENSIONS, upload_picture
from .validation import row_errors
from .workbook import OfficialWorkbook

MAX_IMAGE = 10 * 1024 * 1024
MAX_IMAGES = 200
PHOTOS = 'photos'
PHOTO_COLUMN = 'K'
# Stand-in for the URLs while rows are checked before any upload.
PENDING_URL = 'https://pending.invalid/photo'


class Image:
    """One uploaded image file, kept in memory for the request."""

    def __init__(self, name: str, raw: bytes, mime: str):
        self.name = name
        self.raw = raw
        self.mime = mime


def check_images(images: List[Image]) -> Dict[str, Image]:
    """Refuse the request before reading rows when the files cannot be used."""
    if len(images) > MAX_IMAGES:
        raise RequestRejected(f'Send at most {MAX_IMAGES} images per request.', 413, 'TOO_MANY_IMAGES')
    named: Dict[str, Image] = {}
    for position, image in enumerate(images, start=1):
        if not image.name:
            raise RequestRejected(f'Image {position} has no file name.', 400, 'IMAGE_WITHOUT_NAME')
        if image.name in named:
            raise RequestRejected(f'Image {position} repeats the name of another image.', 400, 'DUPLICATE_IMAGE_NAME')
        if not image.raw:
            raise RequestRejected(f'Image {position} is empty.', 400, 'EMPTY_IMAGE')
        if len(image.raw) > MAX_IMAGE:
            raise RequestRejected(f'Image {position} exceeds 10 MB.', 413, 'IMAGE_TOO_LARGE')
        if image.mime not in MIME_EXTENSIONS:
            raise RequestRejected(f'Image {position} must be PNG, JPEG or WEBP.', 415, 'UNSUPPORTED_IMAGE')
        named[image.name] = image
    return named


def _photo_list(index: int, row: Dict[str, Any], images: Dict[str, Image]) -> Tuple[List[str], Optional[str]]:
    """Return the row's photo names and the K error, if any. Never echoes a name."""
    if PHOTOS not in row:
        return [], None
    names = row[PHOTOS]
    prefix = f'Listing row {index}, column {PHOTO_COLUMN}'
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
        return [], f'{prefix}: photos must be a non-empty list of image file names.'
    if str(row.get(PHOTO_COLUMN) or '').strip():
        return [], f'{prefix}: send either K or photos, not both.'
    if len(set(names)) != len(names):
        return [], f'{prefix}: photos lists the same image twice.'
    for position, name in enumerate(names, start=1):
        if name not in images:
            return [], f'{prefix}: photo {position} was not sent in images.'
    return names, None


def plan_rows(wb: OfficialWorkbook, rows: List[Any], images: Dict[str, Image]) -> List[List[str]]:
    """Check every row and photo reference. Return each row's photo names.

    Raises ValidationError with every problem found; nothing is uploaded yet.
    """
    capacity = wb.profile.last_row - wb.profile.first_row + 1
    if not 1 <= len(rows) <= capacity:
        raise ValidationError(f'The template supports between 1 and {capacity} rows per batch.')

    photos: List[List[str]] = []
    checked: List[Any] = []
    photo_errors: Dict[int, str] = {}
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            photos.append([])
            checked.append(row)
            continue
        names, error = _photo_list(index, row, images)
        photos.append(names)
        copy = {key: value for key, value in row.items() if key != PHOTOS}
        if error:
            photo_errors[index] = error
            copy.pop(PHOTO_COLUMN, None)
            copy[PHOTO_COLUMN] = PENDING_URL
        elif names:
            copy[PHOTO_COLUMN] = ','.join(f'{PENDING_URL}{n}' for n in range(len(names)))
        checked.append(copy)

    errors = [
        item for item in row_errors(wb.profile, checked)
        if not (item['column'] == PHOTO_COLUMN and item['row'] in photo_errors)
    ]
    errors.extend({'row': row, 'column': PHOTO_COLUMN, 'message': message} for row, message in photo_errors.items())
    if errors:
        order = {column: position for position, column in enumerate(wb.profile.editable)}
        errors.sort(key=lambda item: (item['row'], order.get(item['column'], -1)))
        raise ValidationError(errors[0]['message'], errors=errors)
    return photos


class PhotoUploadFailed(MarketplaceError):
    """An upload failed after others succeeded. No workbook is returned."""

    def __init__(self, message: str, code: str, uploaded: int):
        super().__init__(message, code)
        self.uploaded = uploaded


def build(template: bytes, filename: str, rows: List[Any], images: List[Image],
          token: Optional[str], transport=None) -> Tuple[bytes, int]:
    """Return the filled workbook and how many images were uploaded."""
    named = check_images(images)
    wb = OfficialWorkbook(template, filename=filename)
    photos = plan_rows(wb, rows, named)

    order: List[str] = []
    for names in photos:
        order.extend(name for name in names if name not in order)
    if order and not (token or '').strip():
        raise RequestRejected(
            'Authorization header with a Mercado Libre access token is required to upload photos.',
            401, 'ML_TOKEN_MISSING')

    urls: Dict[str, str] = {}
    for position, name in enumerate(order, start=1):
        image = named[name]
        try:
            urls[name] = upload_picture(token, image.raw, image.mime, transport)['url']
        except MarketplaceError as error:
            if error.code.startswith('ML_TOKEN'):
                raise
            raise PhotoUploadFailed(
                f'Upload of image "{name}" failed after {len(urls)} of {len(order)} uploads. '
                f'No workbook was written. {error}',
                error.code, len(urls)) from error

    filled = []
    for row, names in zip(rows, photos):
        row = {key: value for key, value in row.items() if key != PHOTOS}
        if names:
            row[PHOTO_COLUMN] = ','.join(urls[name] for name in names)
        filled.append(row)
    return wb.export(filled), len(order)
