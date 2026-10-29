"""Surgical manipulation and population of official Mercado Libre bulk listing workbooks.

All workbook components outside editable cells (formula engines, hidden auxiliary sheets,
and OpenXML data validations) are preserved to ensure 100% compatibility with Mercado Libre's importer.
"""
import hashlib
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional
from xml.sax.saxutils import escape

from .exceptions import ValidationError
from .size_guides import read_size_guides

CONTRACT_PATH = Path(__file__).with_name('listing_contract.json')
CONTRACT: Dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding='utf8'))

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
SHEET = 'xl/worksheets/sheet3.xml'
CONTROLS = {'B', 'T', 'AM', 'AN', 'AO'}
EDITABLE = [c['coluna'] for c in CONTRACT['colunas'] if c['coluna'] not in CONTROLS]
COMMON = [c for c in EDITABLE if c not in {'A', 'D', 'G', 'H', 'K', 'L', 'M', 'N'}]
MAX_TEMPLATE = 32 * 1024 * 1024


def digest(raw: bytes) -> str:
    """Compute the SHA-256 digest of the given binary payload."""
    return hashlib.sha256(raw).hexdigest()


def column_number(name: str) -> int:
    """Convert an Excel column letter identifier (e.g. 'A', 'Z', 'AA') to 1-based integer index."""
    result = 0
    for c in name.upper():
        result = result * 26 + ord(c) - 64
    return result


class OfficialWorkbook:
    """Represents an official Mercado Libre Excel workbook template."""

    def __init__(self, raw: bytes, expected_hash: Optional[str] = None):
        if len(raw) > MAX_TEMPLATE:
            raise ValidationError('Uploaded file exceeds the maximum allowed limit of 32MB.')

        if expected_hash and digest(raw) != expected_hash:
            raise ValidationError('Workbook hash differs from the expected contract hash.')

        self.raw = raw
        self.sha256 = digest(raw)

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as z:
                if sum(i.file_size for i in z.infolist()) > 64 * 1024 * 1024 or z.testzip():
                    raise ValidationError('Corrupted workbook or invalid compressed data.')

                # Read shared strings table
                shared_strings_xml = (
                    z.read('xl/sharedStrings.xml')
                    if 'xl/sharedStrings.xml' in z.namelist()
                    else b'<sst></sst>'
                )
                strings = [''.join(t.itertext()) for t in ET.fromstring(shared_strings_xml)]

                def extract_cell_values(part_name: str) -> Dict[str, str]:
                    if part_name not in z.namelist():
                        return {}
                    res = {}
                    for cell in ET.fromstring(z.read(part_name)).findall('.//s:c', NS):
                        val = cell.findtext('s:v', '', NS)
                        if cell.get('t') == 'inlineStr':
                            val = ''.join(t.text or '' for t in cell.findall('s:is/s:t', NS))
                        elif cell.get('t') == 's' and val.isdigit() and int(val) < len(strings):
                            val = strings[int(val)]
                        res[cell.get('r')] = val
                    return res

                # Sheet 2 ('extra info') contains dropdown option lists and validity date
                extra = extract_cell_values('xl/worksheets/sheet2.xml')
                self.expires = extra.get('B1', '')
                if self.expires:
                    try:
                        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.expires):
                            raise ValueError('Invalid date format')
                        date.fromisoformat(self.expires)
                    except ValueError:
                        self.expires = ''

                if SHEET not in z.namelist():
                    raise ValidationError(f'Data sheet ({SHEET}) not found in the workbook.')

                self.xml = z.read(SHEET).decode('utf8')
                sheet = ET.fromstring(self.xml)
                first_row = extract_cell_values(SHEET)

                self.fields = [
                    {**field, 'campo': first_row.get(f"{field['coluna']}3") or field['campo']}
                    for field in CONTRACT['colunas']
                ]

                self.default_fields = {}
                for col in COMMON:
                    value = first_row.get(f'{col}8', '')
                    if value in ('', 'Selecionar', 'Insira ou selecione um valor'):
                        continue
                    if col in {'Q', 'Y'} and re.fullmatch(r'\d+\.0+', value):
                        value = value.split('.')[0]
                    self.default_fields[col] = value

                self.options, self.strict = {}, set()
                for validation in sheet.findall('.//s:dataValidation', NS):
                    if validation.get('type') != 'list':
                        continue
                    sqref = validation.get('sqref', '')
                    match_col = re.match(r'[A-Z]+', sqref)
                    if not match_col:
                        continue
                    col = match_col[0]
                    formula = validation.findtext('s:formula1', '', NS)
                    match_formula = re.fullmatch(r"'extra info'!\$([A-Z]+)\$(\d+):\$([A-Z]+)\$(\d+)", formula)
                    if not match_formula:
                        continue
                    first, row, last, _ = match_formula.groups()
                    self.options[col] = [
                        val for ref, val in extra.items()
                        if re.search(r'\d+$', ref)[0] == row
                        and column_number(first) <= column_number(re.match(r'[A-Z]+', ref)[0]) <= column_number(last)
                        and val not in ('Selecionar', 'Insira ou selecione um valor', '')
                    ]
                    if validation.get('showErrorMessage') == 'true':
                        self.strict.add(col)

                guide = extract_cell_values('xl/worksheets/sheet4.xml')
                self.guides, self.guide_columns = read_size_guides(guide)

        except zipfile.BadZipFile as e:
            raise ValidationError('Uploaded file is not a valid zip/xlsx archive.') from e
        except ET.ParseError as e:
            raise ValidationError('Failed to parse workbook XML structures.') from e

    def metadata(self) -> Dict[str, Any]:
        """Return inspected metadata from the official workbook template."""
        return {
            'sha256': self.sha256,
            'sheet': CONTRACT['aba'],
            'filename': CONTRACT['arquivo'],
            'expires': self.expires,
            'default_fields': self.default_fields,
            'options': self.options,
            'strict': sorted(self.strict),
            'guides': self.guides,
            'guide_columns': self.guide_columns,
            'fields': self.fields
        }

    def export(self, rows: List[Dict[str, Any]]) -> bytes:
        """Surgically populate listing rows starting at row 8 without altering formula dependencies."""
        max_rows = CONTRACT['ultima_linha_preparada'] - 7
        if not (1 <= len(rows) <= max_rows):
            raise ValidationError(f'The template supports between 1 and {max_rows} rows per batch.')

        def replace_row(match: re.Match) -> str:
            number = int(match[1])
            if not (8 <= number <= 1001):
                return match[0]

            opening = match[0][:match[0].index('>') + 1]
            cells = {
                re.search(r'\br="([A-Z]+)\d+"', c)[1]: c
                for c in re.findall(r'<c\b[^>]*(?:/>|>.*?</c>)', match[2], re.S)
            }
            row = rows[number - 8] if (number - 8 < len(rows)) else {}

            for col, cell in list(cells.items()):
                if '<f' in cell:
                    cells[col] = re.sub(r'<v(?:\s[^>]*)?>.*?</v>|<v\s*/>', '', cell, flags=re.S)

            for col in EDITABLE:
                old = cells.get(col, '')
                style = re.search(r'\bs="(\d+)"', old)
                attrs = f'r="{col}{number}"' + (f' s="{style[1]}"' if style else '')
                value = row.get(col, '')

                if value is None or value == '':
                    cells[col] = f'<c {attrs}/>'
                elif col in {'N', 'O', 'Q', 'Y'}:
                    cells[col] = f'<c {attrs}><v>{escape(str(value))}</v></c>'
                else:
                    # inlineStr preserves leading zeros on GTINs and literal strings
                    cells[col] = f'<c {attrs} t="inlineStr"><is><t xml:space="preserve">{escape(str(value))}</t></is></c>'

            sorted_cells = ''.join(cells[c] for c in sorted(cells, key=column_number))
            return f'{opening}{sorted_cells}</row>'

        xml = re.sub(r'<row\b[^>]*\br="(\d+)"[^>]*>(.*?)</row>', replace_row, self.xml, flags=re.S)
        ET.fromstring(xml)

        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(self.raw)) as src, zipfile.ZipFile(output, 'w') as dest:
            for item in src.infolist():
                content = src.read(item.filename)
                if item.filename == SHEET:
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
            dest.comment = src.comment

        return output.getvalue()
