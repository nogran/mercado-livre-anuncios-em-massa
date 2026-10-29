"""Parser for the official Mercado Libre size chart sheet (sheet4)."""
import re
from typing import Dict, Any, List, Tuple


def read_size_guides(cells: Dict[str, str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    """Extract measurement definitions and size rows from the size guide sheet."""
    columns = [
        {'key': match[1], 'label': value}
        for ref, value in cells.items()
        if (match := re.fullmatch(r'([A-Z]+)4', ref))
        and (len(match[1]) > 1 or match[1] >= 'G') and value
    ]
    columns.sort(key=lambda col: (len(col['key']), col['key']))

    rows = sorted(
        int(match[1]) for ref, value in cells.items()
        if (match := re.fullmatch(r'A(\d+)', ref)) and int(match[1]) >= 6 and value
    )

    guides = [
        {
            'code': cells.get(f'A{i}', ''),
            'name': cells.get(f'B{i}', ''),
            'gender': cells.get(f'D{i}', ''),
            'size': cells.get(f'E{i}', ''),
            'equivalences': cells.get(f'F{i}', ''),
            'measurements': {col['key']: cells.get(f"{col['key']}{i}", '') for col in columns}
        }
        for i in rows
    ]
    return guides, columns
