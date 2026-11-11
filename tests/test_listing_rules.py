"""Seller rules for the verified Camisetas e Regatas layout.

Each rule is shown accepting a valid row and refusing a broken one. The
refusal names the column and never repeats the submitted value.
"""
import json

import pytest

from app import listing_rules
from app.exceptions import ValidationError
from app.workbook import OfficialWorkbook
from tests.test_workbook import LATER_SAMPLE, LOCAL_SAMPLE, completed_row

VALID_GTIN = '4006381333931'


@pytest.fixture
def wb():
    return OfficialWorkbook(LOCAL_SAMPLE.read_bytes())


def problems(wb, **overrides):
    """Return ``{column: message}`` for one row, or ``{}`` when it is accepted."""
    try:
        wb.export([completed_row(wb, **overrides)])
    except ValidationError as error:
        return {item['column']: item['message'] for item in error.errors}
    return {}


def test_both_camiseta_downloads_use_the_seller_rules():
    for sample in (LOCAL_SAMPLE, LATER_SAMPLE):
        assert listing_rules.applies(OfficialWorkbook(sample.read_bytes()).profile)


def test_other_layout_keeps_only_sheet_rules(wb):
    wb.profile.fields = [dict(field, campo='Outro campo') if field['coluna'] == 'O' else field
                         for field in wb.profile.fields]
    assert not listing_rules.applies(wb.profile)
    assert problems(wb, O=1) == {}


def test_title_counts_excel_characters(wb):
    assert problems(wb, A='a' * 60) == {}
    assert problems(wb, A='a' * 61) == {'A': 'Listing row 1, column A exceeds 60 Excel characters.'}
    # An emoji takes two Excel characters.
    assert 'A' in problems(wb, A='a' * 59 + '\U0001F600')


@pytest.mark.parametrize('price', [8, '8.00', 49.9, '1000000'])
def test_price_accepted(wb, price):
    assert problems(wb, O=price) == {}


@pytest.mark.parametrize('price', [7.99, '1000000.01', '10.999'])
def test_price_refused(wb, price):
    assert problems(wb, O=price) == {
        'O': 'Listing row 1, column O must be between 8 and 1000000 with at most two decimals.'
    }


def test_sheet_rule_wins_on_the_same_column(wb):
    assert problems(wb, O=0) == {'O': 'Listing row 1, column O must be greater than 0.'}


def test_whole_numbers(wb):
    assert problems(wb, N=0, Q='', Y='') == {}
    assert problems(wb, Q=3) == {}
    assert problems(wb, Q='2.5') == {'Q': 'Listing row 1, column Q must be a whole number between 0 and 1000000.'}
    assert problems(wb, Q=1000001) == {'Q': 'Listing row 1, column Q must be a whole number between 0 and 1000000.'}


def test_gtin(wb):
    assert problems(wb, L=VALID_GTIN) == {}
    assert problems(wb, L=wb.profile.options['L'][2]) == {}
    assert problems(wb, L='4006381333932') == {'L': 'Listing row 1, column L has a wrong GTIN check digit.'}
    refused = problems(wb, L='abc123')
    assert refused == {'L': 'Listing row 1, column L must be a GTIN with 8 to 14 digits or a reason from the list.'}
    assert 'abc123' not in json.dumps(refused)


def test_kit_needs_units(wb):
    assert problems(wb, P='Kit', Q=3) == {}
    assert problems(wb, P='Unidade') == {}
    assert problems(wb, P='Kit') == {'Q': 'Listing row 1, column Q must hold the units in the kit.'}
    assert problems(wb, P='Kit', Q=0) == {'Q': 'Listing row 1, column Q must hold the units in the kit.'}


def test_warranty_pairs(wb):
    seller = wb.profile.options['X'][0]
    message = 'Listing row 1, column Y and column Z must hold the warranty length and unit.'
    assert problems(wb, X=seller, Y=3, Z='meses') == {}
    assert problems(wb, X='Sem garantia') == {}
    assert problems(wb, X=seller, Y=3) == {'Y': message}
    assert problems(wb, X=seller, Y=0, Z='meses') == {'Y': message}
    assert problems(wb, Y=3, Z='meses') == {'X': 'Listing row 1, column X must hold the warranty type before its length.'}


@pytest.mark.parametrize('description', [
    '<b>Camiseta</b>', 'Veja https://exemplo.test', 'www.exemplo.test',
    'contato@exemplo.test', 'Ligue 11 99999-0000', 'Camiseta \U0001F600',
])
def test_description_refused(wb, description):
    refused = problems(wb, R=description)
    assert refused == {'R': 'Listing row 1, column R must be plain text without HTML, emoji, links or contact data.'}
    assert description not in json.dumps(refused)


def test_description_plain_text(wb):
    assert problems(wb, R='Camiseta de algodao, malha 30.1, gola careca.') == {}


def test_guide_gender_and_size(wb):
    guide = wb.profile.guides[0]
    other = next(option for option in wb.profile.options['I'] if option != guide['gender'])
    fits = dict(J=guide['code'], I=guide['gender'], G=guide['size'])
    assert problems(wb, **fits) == {}
    assert problems(wb, **dict(fits, I=other)) == {
        'I': 'Listing row 1, column I is not a gender of the size guide in column J.'
    }
    wrong_size = problems(wb, **dict(fits, G='XGG'))
    assert wrong_size == {'G': 'Listing row 1, column G is not a size of the size guide in column J.'}
    assert guide['name'] not in json.dumps(wrong_size)


def test_guide_outside_the_sheet_is_not_matched(wb):
    assert problems(wb, J='1000002', G='XGG') == {}


def test_all_rules_reported_together(wb):
    found = problems(wb, A='a' * 61, O=5, L='123', P='Kit', R='<i>x</i>')
    assert list(found) == ['A', 'L', 'O', 'Q', 'R']
