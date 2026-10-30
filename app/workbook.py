"""Surgical population of official Mercado Libre bulk-listing workbooks.

The editable profile is discovered from the uploaded file. Formula cells,
hidden columns, auxiliary sheets, and data validations are left in place.
"""
import hashlib
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

from .exceptions import ValidationError
from .profile import column_number, read_profile
from .validation import validate_rows

MAX_TEMPLATE = 32 * 1024 * 1024


def digest(raw: bytes) -> str:
    """Compute the SHA-256 digest of the given binary payload."""
    return hashlib.sha256(raw).hexdigest()


class OfficialWorkbook:
    """Official Mercado Libre workbook with a profile read from the file itself."""

    def __init__(self, raw: bytes, filename: Optional[str] = None, expected_hash: Optional[str] = None):
        if len(raw) > MAX_TEMPLATE:
            raise ValidationError('Uploaded file exceeds the maximum allowed limit of 32MB.')
        if expected_hash and digest(raw) != expected_hash:
            raise ValidationError('Workbook hash differs from the expected contract hash.')

        self.raw = raw
        self.filename = filename or ''
        self.sha256 = digest(raw)

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 64 * 1024 * 1024 or archive.testzip():
                    raise ValidationError('Corrupted workbook or invalid compressed data.')
                self.profile = read_profile(archive)
                self.xml = archive.read(self.profile.sheet_part).decode('utf8')
        except zipfile.BadZipFile as error:
            raise ValidationError('Uploaded file is not a valid zip/xlsx archive.') from error
        except ET.ParseError as error:
            raise ValidationError('Failed to parse workbook XML structures.') from error

    def metadata(self) -> Dict[str, Any]:
        """Return the profile discovered from this workbook."""
        profile = self.profile
        return {
            'sha256': self.sha256,
            'sheet': profile.sheet_name,
            'filename': self.filename,
            'expires': profile.expires,
            'first_row': profile.first_row,
            'last_row': profile.last_row,
            'numeric_columns': profile.numeric_columns,
            'text_limits': profile.text_limits,
            'number_rules': profile.number_rules,
            'default_fields': profile.default_fields,
            'options': profile.options,
            'strict': profile.strict,
            'guides': profile.guides,
            'guide_columns': profile.guide_columns,
            'fields': profile.fields,
        }

    def export(self, rows: List[Dict[str, Any]]) -> bytes:
        """Write listing rows into the prepared data window without touching formulas."""
        first = self.profile.first_row
        last = self.profile.last_row
        capacity = last - first + 1
        if not 1 <= len(rows) <= capacity:
            raise ValidationError(f'The template supports between 1 and {capacity} rows per batch.')
        validate_rows(self.profile, rows)

        editable = self.profile.editable
        numeric = set(self.profile.numeric_columns)

        def replace_row(match: re.Match) -> str:
            number = int(match[1])
            if not first <= number <= last:
                return match[0]

            opening = match[0][:match[0].index('>') + 1]
            cells = {
                re.search(r'\br="([A-Z]+)\d+"', cell)[1]: cell
                for cell in re.findall(r'<c\b[^>]*(?:/>|>.*?</c>)', match[2], re.S)
            }
            row = rows[number - first] if number - first < len(rows) else {}

            for column, cell in list(cells.items()):
                if '<f' in cell:
                    cells[column] = re.sub(r'<v(?:\s[^>]*)?>.*?</v>|<v\s*/>', '', cell, flags=re.S)

            for column in editable:
                old = cells.get(column, '')
                style = re.search(r'\bs="(\d+)"', old)
                attrs = f'r="{column}{number}"' + (f' s="{style[1]}"' if style else '')
                value = row.get(column, '')
                if value is None or value == '':
                    cells[column] = f'<c {attrs}/>'
                elif column in numeric:
                    cells[column] = f'<c {attrs}><v>{escape(str(value))}</v></c>'
                else:
                    cells[column] = (
                        f'<c {attrs} t="inlineStr"><is><t xml:space="preserve">'
                        f'{escape(str(value))}</t></is></c>'
                    )

            sorted_cells = ''.join(cells[column] for column in sorted(cells, key=column_number))
            return f'{opening}{sorted_cells}</row>'

        xml = re.sub(r'<row\b[^>]*\br="(\d+)"[^>]*>(.*?)</row>', replace_row, self.xml, flags=re.S)
        ET.fromstring(xml)

        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.raw)) as source, zipfile.ZipFile(output, 'w') as dest:
            for item in source.infolist():
                content = source.read(item.filename)
                if item.filename == self.profile.sheet_part:
                    content = xml.encode('utf8')
                elif item.filename == 'xl/workbook.xml':
                    workbook = content.decode('utf8')
                    calc = '<calcPr calcMode="auto" fullCalcOnLoad="1" forceFullCalc="1"/>'
                    if '<calcPr' in workbook:
                        workbook = re.sub(r'<calcPr\b[^>]*(?:/>|>.*?</calcPr>)', calc, workbook)
                    else:
                        workbook = workbook.replace('</workbook>', calc + '</workbook>')
                    content = workbook.encode('utf8')
                dest.writestr(item, content)
            dest.comment = source.comment
        return output.getvalue()
