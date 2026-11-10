"""Row checks taken from the workbook profile.

Messages name the column and the rule. They do not repeat the submitted
value, so a rejection cannot echo account-specific cell text.
"""
import math
import re
from typing import Any, Dict, List, Optional

from . import listing_rules
from .exceptions import ValidationError
from .profile import WorkbookProfile

_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')


def validate_rows(profile: WorkbookProfile, rows: List[Dict[str, Any]]) -> None:
    """Reject listing rows that break required markers or data validations.

    Every row and column is checked. The raised error carries all problems,
    one per column at most, in row and column order.
    """
    errors = row_errors(profile, rows)
    if errors:
        raise ValidationError(errors[0]['message'], errors=errors)


def row_errors(profile: WorkbookProfile, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return every problem as ``{"row", "column", "message"}``."""
    editable = set(profile.editable)
    required = {
        field['coluna'] for field in profile.fields
        if field['obrigatorio'] and field['coluna'] in editable
    }
    strict = set(profile.strict)
    seller_rules = listing_rules.applies(profile)
    order = {column: position for position, column in enumerate(profile.editable)}
    errors: List[Dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append(_error(index, None, f'Listing row {index} must be an object.'))
            continue
        found: Dict[str, str] = {}
        for column in profile.editable:
            message = _column_error(profile, strict, required, index, column, row.get(column))
            if message:
                found[column] = message
        if seller_rules:
            for column, message in listing_rules.check(profile, index, row):
                found.setdefault(column, message)
        for column in sorted(found, key=lambda col: order.get(col, len(order))):
            errors.append(_error(index, column, found[column]))
    return errors


def _column_error(profile, strict, required, index: int, column: str, value: Any) -> Optional[str]:
    if _blank(value):
        return f'Listing row {index}, column {column} is required.' if column in required else None
    if column in strict and not _accepted_option(column, value, profile):
        return f'Listing row {index}, column {column} is not an allowed option.'
    limit = profile.text_limits.get(column)
    if limit is not None and len(str(value)) > limit:
        return f'Listing row {index}, column {column} exceeds {limit} characters.'
    rule = profile.number_rules.get(column)
    if rule is not None:
        try:
            _check_number(index, column, value, rule)
        except ValidationError as error:
            return str(error)
    return None


def _error(row: int, column: Optional[str], message: str) -> Dict[str, Any]:
    return {'row': row, 'column': column, 'message': message}


def _accepted_option(column: str, value: Any, profile: WorkbookProfile) -> bool:
    """Column J lists sample guides. A numeric code from the seller account is still valid."""
    if str(value) in profile.options.get(column, []):
        return True
    if column != 'J':
        return False
    code = str(value).split(' - ')[0]
    return re.fullmatch(r'[0-9]+', code) is not None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == '')


def _parse_number(index: int, column: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError(f'Listing row {index}, column {column} must be a number.')
    if isinstance(value, str):
        if not _NUMBER.fullmatch(value):
            raise ValidationError(f'Listing row {index}, column {column} must be a number.')
        number = float(value)
    else:
        number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValidationError(f'Listing row {index}, column {column} must be a number.')
    return number


def _check_number(index: int, column: str, value: Any, rule: Dict[str, Any]) -> None:
    number = _parse_number(index, column, value)
    low = rule['low']
    high = rule['high']
    if rule['operator'] == 'greaterThan' and not number > low:
        raise ValidationError(f'Listing row {index}, column {column} must be greater than {format(low, "g")}.')
    if rule['operator'] == 'between' and not low <= number <= high:
        raise ValidationError(
            f'Listing row {index}, column {column} must be between {format(low, "g")} and {format(high, "g")}.'
        )
