"""Unit tests for the OfficialWorkbook engine and the FastAPI server endpoints.

The fixtures are Camisetas e Regatas workbooks with the same structure as a
Mercado Libre download. Account-specific guide labels are generic. The 09-06
file matches the audited column contract; the 09-14 file is a later download
of the same category.
"""
import io
import json
import re
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.exceptions import ValidationError
from app.workbook import OfficialWorkbook
from app.server import app

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
LOCAL_SAMPLE = FIXTURES / 'camiseta-09-06.xlsx'
LATER_SAMPLE = FIXTURES / 'camiseta-09-14.xlsx'
CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'app' / 'listing_contract.json').read_text(encoding='utf8'))
NON_EDITABLE = {'B', 'T', 'AM', 'AN', 'AO'}


def _sample_number(rule):
    if rule['operator'] == 'greaterThan':
        return rule['low'] + 1
    if rule['low'] <= 1 <= rule['high']:
        return 1
    return rule['low']


def completed_row(wb, **overrides):
    """Fill required columns from the template rules.

    Strict lists use the first allowed option already inside the workbook.
    That value stays in memory for the assertion and is never written here.
    """
    profile = wb.profile
    required = {
        field['coluna'] for field in profile.fields
        if field['obrigatorio'] and field['coluna'] in profile.editable
    }
    row = {}
    for column in profile.editable:
        if column not in required:
            continue
        if column in profile.strict:
            choices = profile.options.get(column) or []
            if not choices:
                raise AssertionError(f'column {column} has no allowed option')
            row[column] = choices[0]
        elif column in profile.number_rules:
            row[column] = _sample_number(profile.number_rules[column])
        else:
            row[column] = 'item'
    row.update(overrides)
    return row


@pytest.fixture
def sample_bytes():
    return LOCAL_SAMPLE.read_bytes()


def test_official_workbook_inspect(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    meta = wb.metadata()

    assert meta['sheet'] == 'Camisetas e Regatas'
    assert len(meta['fields']) > 0
    assert 'C' in meta['options']
    assert 'Novo' in meta['options']['C']
    assert meta['first_row'] == 8
    assert meta['last_row'] == 1001
    assert meta['numeric_columns'] == ['N', 'O', 'Q', 'Y']
    assert meta['text_limits'] == {'M': 255, 'R': 50000, 'AA': 255}
    assert meta['number_rules']['O']['operator'] == 'greaterThan'


def test_profile_matches_verified_camiseta_contract(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    assert wb.sha256 != CONTRACT['sha256']
    assert {guide['code'] for guide in wb.profile.guides} == {'1000001'}
    assert {guide['name'] for guide in wb.profile.guides} == {'Guia 1'}
    assert wb.profile.sheet_name == CONTRACT['aba']
    assert wb.profile.sheet_part == 'xl/worksheets/sheet3.xml'
    assert wb.profile.first_row == CONTRACT['primeira_linha']
    assert wb.profile.last_row == CONTRACT['ultima_linha_preparada']
    assert wb.profile.expires == CONTRACT['prazo_modelo']
    assert wb.profile.fields == CONTRACT['colunas']
    assert len(wb.profile.guides) == 5
    assert wb.profile.editable == [
        column['coluna'] for column in CONTRACT['colunas'] if column['coluna'] not in NON_EDITABLE
    ]
    assert wb.profile.text_limits == {'M': 255, 'R': 50000, 'AA': 255}
    assert wb.profile.number_rules['N'] == {'operator': 'between', 'low': 0.0, 'high': 99999.0}
    assert wb.profile.number_rules['O'] == {'operator': 'greaterThan', 'low': 0.0, 'high': None}


def test_export_keeps_formula_columns_and_writes_numbers(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    output = wb.export([completed_row(wb, A='Literal title', B='999', O=59.90, AO='hidden')])
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        sheet_xml = archive.read(wb.profile.sheet_part).decode('utf8')
    row = re.search(r'<row\b[^>]*\br="8"[^>]*>.*?</row>', sheet_xml, re.S).group(0)
    quantity = re.search(r'<c\b[^>]*\br="B8"[^>]*(?:/>|>.*?</c>)', row, re.S).group(0)
    price = re.search(r'<c\b[^>]*\br="O8"[^>]*(?:/>|>.*?</c>)', row, re.S).group(0)
    hidden = re.search(r'<c\b[^>]*\br="AO8"[^>]*(?:/>|>.*?</c>)', row, re.S)
    assert '<f' in quantity
    assert '999' not in quantity
    assert '<v>59.9</v>' in price
    assert 'inlineStr' not in price
    assert hidden is None or 'hidden' not in hidden.group(0)
    assert 'Literal title' in row


@pytest.fixture
def later_bytes():
    return LATER_SAMPLE.read_bytes()


def test_later_camiseta_template_uses_its_own_profile(later_bytes):
    wb = OfficialWorkbook(later_bytes)
    labels = {field['coluna']: field['campo'] for field in wb.profile.fields}
    frozen = {field['coluna']: field['campo'] for field in CONTRACT['colunas']}
    assert wb.profile.sheet_name == 'Camisetas e Regatas'
    assert wb.sha256 != CONTRACT['sha256']
    assert {guide['code'] for guide in wb.profile.guides} == {'1000001'}
    assert {guide['name'] for guide in wb.profile.guides} == {'Guia 1'}
    assert wb.profile.expires == '2026-10-14'
    assert labels['Q'] == 'Quantidade de camisetas'
    assert labels['Q'] != frozen['Q']
    assert wb.profile.number_rules['O']['operator'] == 'greaterThan'
    output = wb.export([completed_row(wb, A='Later camiseta row', Q=2, O=19.9)])
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        sheet_xml = archive.read(wb.profile.sheet_part).decode('utf8')
    assert 'Later camiseta row' in sheet_xml
    assert '<v>2</v>' in sheet_xml


def test_official_workbook_export(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)

    rows = [
        completed_row(wb, A='Anuncio de teste', D='Preto', G='M', O=59.90, N=15),
        completed_row(wb, A='Anuncio de teste', D='Preto', G='G', O=59.90, N=20),
    ]

    output_bytes = wb.export(rows)
    assert len(output_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(output_bytes)) as z:
        assert 'xl/worksheets/sheet3.xml' in z.namelist()
        sheet_xml = z.read('xl/worksheets/sheet3.xml').decode('utf8')
        assert 'Anuncio de teste' in sheet_xml
        assert '59.9' in sheet_xml

        workbook_xml = z.read('xl/workbook.xml').decode('utf8')
        assert 'fullCalcOnLoad="1"' in workbook_xml


def test_api_root():
    client = TestClient(app)
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["project"] == "mercado-livre-anuncios-em-massa"


def test_api_inspect_endpoint(sample_bytes):
    client = TestClient(app)
    response = client.post(
        "/v1/templates/inspect",
        files={"template": ("template.xlsx", sample_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
    )
    assert response.status_code == 200
    meta = response.json()
    assert meta["sheet"] == "Camisetas e Regatas"
    assert meta["first_row"] == 8
    assert meta["filename"] == "template.xlsx"
    assert "C" in meta["options"]


def test_api_fill_endpoint(sample_bytes):
    client = TestClient(app)
    wb = OfficialWorkbook(sample_bytes)
    rows = [completed_row(wb, A='Anuncio de teste', O=49.90, N=10)]

    response = client.post(
        "/v1/workbooks/fill",
        files={"template": ("template.xlsx", sample_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"payload": json.dumps(rows)}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert len(response.content) > 0

    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        sheet_xml = z.read('xl/worksheets/sheet3.xml').decode('utf8')
        assert 'Anuncio de teste' in sheet_xml


def test_fill_rejects_sheet_rules_without_echoing_the_value(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    missing = completed_row(wb, A='   ')
    with pytest.raises(ValidationError) as missing_error:
        wb.export([missing])
    assert str(missing_error.value) == 'Listing row 1, column A is required.'

    too_long = completed_row(wb, M='x' * 256)
    with pytest.raises(ValidationError) as length_error:
        wb.export([too_long])
    assert str(length_error.value) == 'Listing row 1, column M exceeds 255 characters.'
    assert 'xxx' not in str(length_error.value)

    free_price = completed_row(wb, O=0)
    with pytest.raises(ValidationError) as price_error:
        wb.export([free_price])
    assert str(price_error.value) == 'Listing row 1, column O must be greater than 0.'

    huge_stock = completed_row(wb, N=100000)
    with pytest.raises(ValidationError) as stock_error:
        wb.export([huge_stock])
    assert str(stock_error.value) == 'Listing row 1, column N must be between 0 and 99999.'

    text_price = completed_row(wb, O='gratis')
    with pytest.raises(ValidationError) as number_error:
        wb.export([text_price])
    assert str(number_error.value) == 'Listing row 1, column O must be a number.'
    assert 'gratis' not in str(number_error.value)

    unknown_condition = completed_row(wb, C='Quase novo')
    with pytest.raises(ValidationError) as option_error:
        wb.export([unknown_condition])
    assert str(option_error.value) == 'Listing row 1, column C is not an allowed option.'
    assert 'Quase novo' not in str(option_error.value)

    output = wb.export([completed_row(wb, D='Cor fora da lista', N=0)])
    assert len(output) > 0


def test_api_fill_rejects_incomplete_row(sample_bytes):
    client = TestClient(app)
    response = client.post(
        "/v1/workbooks/fill",
        files={"template": ("template.xlsx", sample_bytes, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"payload": json.dumps([{}])}
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Listing row 1, column A is required."
