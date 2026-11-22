"""Local files for the MCP server, confined to one work folder.

Every path a tool receives is read or written only inside the work folder
(``MLAM_WORKDIR``, default: the folder the server started in). ``..``, an
absolute path elsewhere and a link that leads out are refused before any
byte is read.
"""
import os
import tempfile
from pathlib import Path
from typing import Union

from .build import MAX_IMAGE
from .workbook import MAX_TEMPLATE

IMAGE_MIMES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp'}


class FileRefused(Exception):
    """A path or file the tool cannot use. ``code`` is the tool error code."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class WorkDir:
    """The one folder the tools may read from and write to."""

    def __init__(self, root: Union[str, Path, None] = None):
        root = root or os.environ.get('MLAM_WORKDIR') or os.getcwd()
        self.root = Path(os.path.realpath(root))
        if not self.root.is_dir():
            raise FileRefused('MLAM_WORKDIR is not a folder.', 'WORKDIR_MISSING')

    def resolve(self, path: str) -> Path:
        """Return the real path inside the work folder, or refuse it."""
        if not isinstance(path, str) or not path.strip():
            raise FileRefused('Path is empty.', 'PATH_EMPTY')
        given = Path(path.strip())
        if '..' in given.parts:
            raise FileRefused(f'Path "{path}" uses "..". Use a path inside the work folder.', 'PATH_OUTSIDE_WORKDIR')
        real = Path(os.path.realpath(given if given.is_absolute() else self.root / given))
        if real != self.root and self.root not in real.parents:
            raise FileRefused(f'Path "{path}" is outside the work folder.', 'PATH_OUTSIDE_WORKDIR')
        return real

    def read(self, path: str, limit: int, kind: str) -> bytes:
        """Read a file of at most ``limit`` bytes."""
        real = self.resolve(path)
        if not real.is_file():
            raise FileRefused(f'{kind} "{path}" was not found in the work folder.', 'FILE_NOT_FOUND')
        if real.stat().st_size > limit:
            raise FileRefused(f'{kind} "{path}" exceeds {limit // (1024 * 1024)} MB.', 'FILE_TOO_LARGE')
        with open(real, 'rb') as handle:
            raw = handle.read(limit + 1)
        if not raw:
            raise FileRefused(f'{kind} "{path}" is empty.', 'EMPTY_FILE')
        if len(raw) > limit:
            raise FileRefused(f'{kind} "{path}" exceeds {limit // (1024 * 1024)} MB.', 'FILE_TOO_LARGE')
        return raw

    def read_template(self, path: str) -> bytes:
        return self.read(path, MAX_TEMPLATE, 'Template')

    def image_mime(self, path: str) -> str:
        """MIME from the extension. Anything but PNG, JPEG or WEBP is refused."""
        mime = IMAGE_MIMES.get(Path(path).suffix.lower())
        if not mime:
            raise FileRefused(f'Photo "{path}" must be .png, .jpg, .jpeg or .webp.', 'INVALID_MIME')
        return mime

    def check_image(self, path: str) -> Path:
        """Refuse a photo path without reading it: location, extension, size."""
        self.image_mime(path)
        real = self.resolve(path)
        if not real.is_file():
            raise FileRefused(f'Photo "{path}" was not found in the work folder.', 'FILE_NOT_FOUND')
        size = real.stat().st_size
        if not size:
            raise FileRefused(f'Photo "{path}" is empty.', 'EMPTY_FILE')
        if size > MAX_IMAGE:
            raise FileRefused(f'Photo "{path}" exceeds 10 MB.', 'FILE_TOO_LARGE')
        return real

    def read_image(self, path: str) -> bytes:
        self.image_mime(path)
        return self.read(path, MAX_IMAGE, 'Photo')

    def output(self, path: str, overwrite: bool) -> Path:
        """Check where a workbook may be written, before any work is done."""
        if Path(path.strip() if isinstance(path, str) else '').suffix.lower() != '.xlsx':
            raise FileRefused('output_path must end in .xlsx.', 'INVALID_OUTPUT')
        real = self.resolve(path)
        if not real.parent.is_dir():
            raise FileRefused(f'The folder of "{path}" does not exist.', 'FILE_NOT_FOUND')
        if real.exists() and not overwrite:
            raise FileRefused(f'"{path}" already exists. Pass overwrite: true to replace it.', 'OUTPUT_EXISTS')
        if real.exists() and not real.is_file():
            raise FileRefused(f'"{path}" is not a file.', 'INVALID_OUTPUT')
        return real

    def write(self, real: Path, raw: bytes) -> None:
        """Write through a temporary file, so a failure leaves no half workbook."""
        handle, temp = tempfile.mkstemp(dir=real.parent, prefix='.mlam-', suffix='.xlsx')
        try:
            with os.fdopen(handle, 'wb') as out:
                out.write(raw)
            os.replace(temp, real)
        except BaseException:
            if os.path.exists(temp):
                os.unlink(temp)
            raise

    def relative(self, real: Path) -> str:
        return real.relative_to(self.root).as_posix()
