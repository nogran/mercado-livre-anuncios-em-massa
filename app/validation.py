"""Row checks taken from the workbook profile.

Messages name the column and the rule. They do not repeat the submitted
value, so a rejection cannot echo account-specific cell text.
"""
import math
import re
from typing import Any, Dict, List

from .exceptions import ValidationError
from .profile import WorkbookProfile

_NUMBER = re.compile(r'-?\d+(?:\.\d+)?')


def validate_rows(profile: WorkbookProfile, rows: List[Dict[str, Any]]) -> None:
    """Reject listing rows that break required markers or data validations."""
    editable = set(profile.editable)
    required = [
        field['coluna'] for field in profile.fields
        if field['obrigatorio'] and field['coluna'] in editable
    ]
    strict = set(profile.strict)
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValidationError(f'Listing row {index} must be an object.')
        for column in required:
            if _blank(row.get(column)):
                raise ValidationError(f'Listing row {index}, column {column} is required.')
        for column in profile.editable:
            value = row.get(column)
            if _blank(value):
                continue
            if column in strict and str(value) not in profile.options.get(column, []):
                raise ValidationError(f'Listing row {index}, column {column} is not an allowed option.')
            limit = profile.text_limits.get(column)
            if limit is not None and len(str(value)) > limit:
                raise ValidationError(f'Listing row {index}, column {column} exceeds {limit} characters.')
            rule = profile.number_rules.get(column)
            if rule is not None:
                _check_number(index, column, value, rule)


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
