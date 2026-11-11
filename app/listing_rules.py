"""Seller rules for the Camisetas e Regatas layout that the sheet itself does not enforce.

The official workbook only carries Excel data validations. Mercado Libre still
rejects a long title, a price under R$ 8, a GTIN with a wrong check digit and a
few column pairs after upload. These checks run before the file is written.

They apply only when the uploaded workbook has the verified layout: each anchor
column must carry the expected header. Another category keeps the sheet rules
alone. Messages never repeat the submitted value nor the seller's guide data.
"""
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from .profile import WorkbookProfile

LAYOUT = {
    'A': 'titulo',
    'G': 'tamanho',
    'I': 'genero',
    'J': 'codigo do guia',
    'L': 'codigo universal',
    'N': 'estoque',
    'O': 'preco',
    'P': 'formato de venda',
    'R': 'descricao',
    'X': 'tipo de garantia',
    'Y': 'tempo de garantia',
    'Z': 'unidade de tempo de garantia',
}

TITLE_LIMIT = 60
PRICE_LOW = Decimal('8')
PRICE_HIGH = Decimal('1000000')
INTEGER_HIGH = 1000000
NO_WARRANTY = 'Sem garantia'
KIT = 'Kit'
DESCRIPTION_FORBIDDEN = re.compile(
    r'<[^>]+>|https?://|www\.|@|\+?\d[\d ()-]{7,}\d|[\U0001F000-\U0001FAFF]')


def _plain(text: str) -> str:
    first = (text or '').splitlines()[0] if text else ''
    folded = unicodedata.normalize('NFKD', first)
    return ''.join(c for c in folded if not unicodedata.combining(c)).strip().lower()


def applies(profile: WorkbookProfile) -> bool:
    """True when the workbook has the verified Camisetas e Regatas columns."""
    headers = {field['coluna']: _plain(field['campo']) for field in profile.fields}
    return all(headers.get(column, '').startswith(prefix) for column, prefix in LAYOUT.items())


def _text(value: Any) -> str:
    return '' if value is None else str(value).strip()


def _integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    text = _text(value)
    return int(text) if re.fullmatch(r'\d+', text) else None


def _gtin_ok(code: str) -> bool:
    body = code[-2::-1]
    total = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(body))
    return total % 10 == (-int(code[-1])) % 10


def check(profile: WorkbookProfile, index: int, row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return ``(column, message)`` for each broken seller rule in one row."""
    found: List[Tuple[str, str]] = []

    def fail(column: str, rule: str) -> None:
        found.append((column, f'Listing row {index}, column {column} {rule}.'))

    title = _text(row.get('A'))
    if title and len(title.encode('utf-16-le')) // 2 > TITLE_LIMIT:
        fail('A', f'exceeds {TITLE_LIMIT} Excel characters')

    price = _text(row.get('O'))
    if price and not isinstance(row.get('O'), bool):
        try:
            number = Decimal(price)
            if not number.is_finite() or not PRICE_LOW <= number <= PRICE_HIGH or number.as_tuple().exponent < -2:
                raise InvalidOperation
        except InvalidOperation:
            fail('O', 'must be between 8 and 1000000 with at most two decimals')

    for column in ('N', 'Q', 'Y'):
        if _text(row.get(column)) == '':
            continue
        number = _integer(row.get(column))
        if number is None or number > INTEGER_HIGH:
            fail(column, f'must be a whole number between 0 and {INTEGER_HIGH}')

    gtin = _text(row.get('L'))
    if gtin and gtin not in profile.options.get('L', []):
        if not re.fullmatch(r'\d{8,14}', gtin):
            fail('L', 'must be a GTIN with 8 to 14 digits or a reason from the list')
        elif not _gtin_ok(gtin):
            fail('L', 'has a wrong GTIN check digit')

    if _text(row.get('P')) == KIT:
        units = _integer(row.get('Q'))
        if units is None or units < 1:
            fail('Q', 'must hold the units in the kit')

    warranty = _text(row.get('X'))
    if warranty and warranty != NO_WARRANTY:
        length = _integer(row.get('Y'))
        if length is None or length < 1 or not _text(row.get('Z')):
            fail('Y', 'and column Z must hold the warranty length and unit')
    elif not warranty and (_text(row.get('Y')) or _text(row.get('Z'))):
        fail('X', 'must hold the warranty type before its length')

    if DESCRIPTION_FORBIDDEN.search(_text(row.get('R'))):
        fail('R', 'must be plain text without HTML, emoji, links or contact data')

    found.extend(_guide(profile, index, row))
    return found


def _guide(profile: WorkbookProfile, index: int, row: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Gender and size must exist in the chosen guide when the workbook lists it.

    A numeric code outside the guide sheet stays accepted without this check:
    the official sheet allows pasting a guide created elsewhere.
    """
    code = _text(row.get('J')).split(' - ')[0]
    gender = _text(row.get('I'))
    if not code or not gender:
        return []
    rows = [guide for guide in profile.guides if guide['code'] == code]
    if not rows:
        return []
    matching = [guide for guide in rows if not guide['gender'] or guide['gender'] == gender]
    if not matching:
        return [('I', f'Listing row {index}, column I is not a gender of the size guide in column J.')]
    size = _text(row.get('G'))
    if size and not any(guide['size'] == size for guide in matching):
        return [('G', f'Listing row {index}, column G is not a size of the size guide in column J.')]
    return []
