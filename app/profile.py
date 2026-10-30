"""Profile read from an official Mercado Libre bulk-listing workbook.

Column letters, headers, required markers, list options, and the data-row
window are taken from the uploaded file. The structure below is the one the
Camisetas e Regatas template actually uses. A file that does not expose it
is rejected. Other categories are not treated as supported until a real
template of that category is tested.
"""
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import ValidationError
from .size_guides import read_size_guides

MAIN = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG = 'http://schemas.openxmlformats.org/package/2006/relationships'
NS = {'s': MAIN}
PLACEHOLDERS = ('', 'Selecionar', 'Insira ou selecione um valor')
# Present on the verified T-shirt template. Absence only means this file has
# no size-guide sheet; it is not evidence about other categories.
GUIDE_SHEET = 'Guias de tamanhos'


@dataclass
class WorkbookProfile:
    """Editable surface discovered from one uploaded workbook."""

    sheet_name: str
    sheet_part: str
    expires: str
    first_row: int
    last_row: int
    fields: List[Dict[str, Any]]
    editable: List[str]
    numeric_columns: List[str]
    text_limits: Dict[str, int]
    number_rules: Dict[str, Dict[str, Any]]
    options: Dict[str, List[str]]
    strict: List[str]
    default_fields: Dict[str, str]
    guides: List[Dict[str, Any]]
    guide_columns: List[Dict[str, str]]


def column_number(name: str) -> int:
    """Convert an Excel column letter (A, Z, AA) to a 1-based index."""
    result = 0
    for char in name.upper():
        result = result * 26 + ord(char) - 64
    return result


def _sheet_entries(archive: zipfile.ZipFile) -> List[Tuple[str, str]]:
    workbook = ET.fromstring(archive.read('xl/workbook.xml'))
    relationships = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
    targets = {
        rel.get('Id'): rel.get('Target')
        for rel in relationships.findall(f'{{{PKG}}}Relationship')
    }
    sheets = []
    for sheet in workbook.findall(f'{{{MAIN}}}sheets/{{{MAIN}}}sheet'):
        target = (targets.get(sheet.get(f'{{{REL}}}id')) or '').lstrip('/')
        if not target:
            raise ValidationError('Workbook sheet relationship is missing.')
        path = target if target.startswith('xl/') else f'xl/{target}'
        sheets.append((sheet.get('name') or '', path))
    return sheets


def _shared_strings(archive: zipfile.ZipFile) -> List[str]:
    if 'xl/sharedStrings.xml' not in archive.namelist():
        return []
    root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
    return [''.join(node.itertext()) for node in root]


def _cell_text(cell: ET.Element, strings: List[str]) -> str:
    value = cell.findtext('s:v', '', NS)
    kind = cell.get('t')
    if kind == 'inlineStr':
        return ''.join(node.text or '' for node in cell.findall('s:is/s:t', NS))
    if kind == 's' and value.isdigit() and int(value) < len(strings):
        return strings[int(value)]
    return value


def _flat_cells(archive: zipfile.ZipFile, part: str, strings: List[str]) -> Dict[str, str]:
    if part not in archive.namelist():
        raise ValidationError(f'Worksheet {part} was not found in the workbook.')
    cells = {}
    root = ET.fromstring(archive.read(part))
    for cell in root.findall('.//s:c', NS):
        cells[cell.get('r')] = _cell_text(cell, strings)
    return cells


def _validation_column(sqref: str) -> str:
    columns = []
    for part in sqref.split():
        match = re.match(r'([A-Z]+)\d+(?::([A-Z]+)\d+)?$', part)
        if not match:
            raise ValidationError(f'Unrecognized validation range: {sqref}')
        columns.append(match.group(1))
        if match.group(2):
            columns.append(match.group(2))
    if len(set(columns)) != 1:
        raise ValidationError(f'Validation range covers more than one column: {sqref}')
    return columns[0]


def _validation_rows(sqref: str) -> Tuple[int, int]:
    rows = []
    for part in sqref.split():
        match = re.match(r'[A-Z]+(\d+)(?::[A-Z]+(\d+))?$', part)
        if not match:
            raise ValidationError(f'Unrecognized validation range: {sqref}')
        rows.append(int(match.group(1)))
        if match.group(2):
            rows.append(int(match.group(2)))
    return min(rows), max(rows)


def _data_sheet(archive: zipfile.ZipFile, sheets: List[Tuple[str, str]]) -> Tuple[str, str, ET.Element]:
    found = []
    for name, path in sheets:
        root = ET.fromstring(archive.read(path))
        validations = root.findall('s:dataValidations/s:dataValidation', NS)
        if validations:
            found.append((name, path, root))
    if len(found) != 1:
        raise ValidationError(
            'Expected exactly one worksheet with data validations. '
            'Only the Camisetas e Regatas template has been verified.'
        )
    return found[0]


def _marker_row(cells_by_row: Dict[int, Dict[str, str]], first_row: int) -> int:
    counts = {}
    for number, columns in cells_by_row.items():
        if number >= first_row:
            continue
        count = sum(1 for value in columns.values() if value.strip() == 'Obrigatório')
        if count:
            counts[number] = count
    if not counts:
        raise ValidationError('Required-field marker row was not found above the data rows.')
    top = max(counts.values())
    winners = [number for number, count in counts.items() if count == top]
    if len(winners) != 1:
        raise ValidationError('Required-field marker row is ambiguous.')
    return winners[0]


def _bound(column: str, raw: str) -> float:
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', raw or ''):
        raise ValidationError(f'Column {column} has a non-numeric validation bound.')
    return float(raw)


def _text_limit(column: str, validation: ET.Element) -> int:
    if validation.get('operator') != 'lessThanOrEqual':
        raise ValidationError(f'Unsupported text length operator on column {column}.')
    limit = _bound(column, validation.findtext('s:formula1', '', NS))
    if limit < 0 or limit != int(limit):
        raise ValidationError(f'Column {column} has an invalid text length.')
    return int(limit)


def _number_rule(column: str, validation: ET.Element) -> Dict[str, Any]:
    operator = validation.get('operator')
    low = _bound(column, validation.findtext('s:formula1', '', NS))
    if operator == 'greaterThan':
        return {'operator': operator, 'low': low, 'high': None}
    if operator == 'between':
        high = _bound(column, validation.findtext('s:formula2', '', NS))
        return {'operator': operator, 'low': low, 'high': high}
    raise ValidationError(f'Unsupported number operator on column {column}.')


def _hidden_columns(sheet: ET.Element) -> set:
    hidden = set()
    for column in sheet.findall('s:cols/s:col', NS):
        if column.get('hidden') != 'true':
            continue
        start = int(column.get('min'))
        end = int(column.get('max'))
        hidden.update(range(start, end + 1))
    return hidden


def read_profile(archive: zipfile.ZipFile) -> WorkbookProfile:
    """Build the editable profile of one official workbook."""
    strings = _shared_strings(archive)
    sheets = _sheet_entries(archive)
    sheet_name, sheet_part, sheet = _data_sheet(archive, sheets)

    options_name: Optional[str] = None
    first_rows = set()
    numeric = set()
    strict = set()
    ranges = {}
    text_limits = {}
    number_rules = {}
    for validation in sheet.findall('s:dataValidations/s:dataValidation', NS):
        sqref = validation.get('sqref') or ''
        column = _validation_column(sqref)
        start, _end = _validation_rows(sqref)
        first_rows.add(start)
        kind = validation.get('type')
        if kind == 'decimal':
            numeric.add(column)
            number_rules[column] = _number_rule(column, validation)
            continue
        if kind == 'textLength':
            text_limits[column] = _text_limit(column, validation)
            continue
        if kind != 'list':
            continue
        formula = validation.findtext('s:formula1', '', NS)
        match = re.fullmatch(r"'([^']+)'!\$([A-Z]+)\$(\d+):\$([A-Z]+)\$(\d+)", formula)
        if not match:
            raise ValidationError(f'List validation for column {column} is not a sheet range.')
        referred, left, row, right, _last = match.groups()
        if options_name not in (None, referred):
            raise ValidationError('List validations point at more than one options sheet.')
        options_name = referred
        ranges[column] = (left, row, right)
        if validation.get('showErrorMessage') == 'true':
            strict.add(column)

    if len(first_rows) != 1:
        raise ValidationError('Data validations do not share one first row.')
    first_row = first_rows.pop()
    if not options_name:
        raise ValidationError('Workbook data sheet has no list validations.')

    options_part = next((path for name, path in sheets if name == options_name), '')
    if not options_part:
        raise ValidationError(f'Options sheet {options_name!r} was not found.')
    option_cells = _flat_cells(archive, options_part, strings)
    expires = option_cells.get('B1', '')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', expires):
        raise ValidationError('Model expiry is missing or invalid. Download the official workbook again.')
    date.fromisoformat(expires)

    options = {}
    for column, (left, row, right) in ranges.items():
        options[column] = [
            value for ref, value in option_cells.items()
            if re.search(r'\d+$', ref) and re.search(r'\d+$', ref)[0] == row
            and column_number(left) <= column_number(re.match(r'[A-Z]+', ref)[0]) <= column_number(right)
            and value not in PLACEHOLDERS
        ]

    cells_by_row: Dict[int, Dict[str, str]] = {}
    formula_columns = set()
    row_numbers = []
    for row in sheet.findall('s:sheetData/s:row', NS):
        number = int(row.get('r'))
        row_numbers.append(number)
        columns = {}
        for cell in row.findall('s:c', NS):
            ref = cell.get('r') or ''
            match = re.match(r'[A-Z]+', ref)
            if not match:
                continue
            letter = match[0]
            columns[letter] = _cell_text(cell, strings)
            if number >= first_row and cell.find('s:f', NS) is not None:
                formula_columns.add(letter)
        cells_by_row[number] = columns

    prepared = sorted(number for number in row_numbers if number >= first_row)
    if not prepared or prepared[0] != first_row or prepared != list(range(first_row, prepared[-1] + 1)):
        raise ValidationError('Prepared data rows are missing or not contiguous.')
    last_row = prepared[-1]

    required_row = _marker_row(cells_by_row, first_row)
    header_row = required_row - 1
    headers = cells_by_row.get(header_row, {})
    if not any(value.strip() for value in headers.values()):
        raise ValidationError('Column header row was not found above the required-field markers.')
    instruction_row = required_row + 1
    instructions = cells_by_row.get(instruction_row, {}) if instruction_row < first_row else {}
    required = cells_by_row.get(required_row, {})
    hidden = _hidden_columns(sheet)

    fields = []
    for letter in sorted(headers, key=column_number):
        label = headers[letter]
        if not label.strip():
            continue
        fields.append({
            'coluna': letter,
            'campo': label,
            'obrigatorio': required.get(letter, '').strip() == 'Obrigatório',
            'instrucao': instructions.get(letter, ''),
        })
    editable = [
        field['coluna'] for field in fields
        if field['coluna'] not in formula_columns and column_number(field['coluna']) not in hidden
    ]
    numeric_columns = [letter for letter in editable if letter in numeric]
    text_limits = {letter: text_limits[letter] for letter in editable if letter in text_limits}
    number_rules = {letter: number_rules[letter] for letter in editable if letter in number_rules}

    prefilled = cells_by_row.get(first_row, {})
    default_fields = {}
    for letter in editable:
        value = prefilled.get(letter, '')
        if value in PLACEHOLDERS:
            continue
        if letter in numeric and re.fullmatch(r'\d+\.0+', value):
            value = value.split('.')[0]
        default_fields[letter] = value

    guide_parts = [path for name, path in sheets if name == GUIDE_SHEET]
    if len(guide_parts) > 1:
        raise ValidationError('Workbook has more than one size-guide sheet.')
    if guide_parts:
        guides, guide_columns = read_size_guides(_flat_cells(archive, guide_parts[0], strings))
    else:
        guides, guide_columns = [], []

    return WorkbookProfile(
        sheet_name=sheet_name,
        sheet_part=sheet_part,
        expires=expires,
        first_row=first_row,
        last_row=last_row,
        fields=fields,
        editable=editable,
        numeric_columns=numeric_columns,
        text_limits=text_limits,
        number_rules=number_rules,
        options=options,
        strict=sorted(strict, key=column_number),
        default_fields=default_fields,
        guides=guides,
        guide_columns=guide_columns,
    )
