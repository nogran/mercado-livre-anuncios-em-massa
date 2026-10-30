"""Unit tests for the OfficialWorkbook engine and the FastAPI server endpoints.

The profile is read from the workbook. These tests only use Camisetas e
Regatas downloads. The 09-06 file is the audited contract; the 09-14 file
is a later download of the same category and must not be forced through
that frozen column map.
"""
import io
import json
import re
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.workbook import OfficialWorkbook
from app.server import app

LOCAL_SAMPLE = Path(__file__).resolve().parents[2] / 'nogran-ml-excel' / 'Anunciar-09-06-10_43_36.xlsx'
LATER_SAMPLE = Path(__file__).resolve().parents[2] / 'nogran-ml-excel' / 'Anunciar-09-14-12_08_33.xlsx'
CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'app' / 'listing_contract.json').read_text(encoding='utf8'))
NON_EDITABLE = {'B', 'T', 'AM', 'AN', 'AO'}


@pytest.fixture
def sample_bytes():
    if not LOCAL_SAMPLE.exists():
        pytest.skip(f"Test template sample not found at {LOCAL_SAMPLE}")
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


def test_profile_matches_verified_camiseta_contract(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    assert wb.sha256 == CONTRACT['sha256']
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


def test_export_keeps_formula_columns_and_writes_numbers(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)
    output = wb.export([{'A': 'Literal title', 'B': '999', 'O': 59.90, 'AO': 'hidden'}])
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
    if not LATER_SAMPLE.exists():
        pytest.skip(f"Later camiseta template not found at {LATER_SAMPLE}")
    return LATER_SAMPLE.read_bytes()


def test_later_camiseta_template_uses_its_own_profile(later_bytes):
    wb = OfficialWorkbook(later_bytes)
    labels = {field['coluna']: field['campo'] for field in wb.profile.fields}
    frozen = {field['coluna']: field['campo'] for field in CONTRACT['colunas']}
    assert wb.profile.sheet_name == 'Camisetas e Regatas'
    assert wb.sha256 != CONTRACT['sha256']
    assert wb.profile.expires == '2026-10-14'
    assert labels['Q'] == 'Quantidade de camisetas'
    assert labels['Q'] != frozen['Q']
    output = wb.export([{'A': 'Later camiseta row', 'Q': 2, 'O': 19.9}])
    with zipfile.ZipFile(io.BytesIO(output)) as archive:
        sheet_xml = archive.read(wb.profile.sheet_part).decode('utf8')
    assert 'Later camiseta row' in sheet_xml
    assert '<v>2</v>' in sheet_xml


def test_official_workbook_export(sample_bytes):
    wb = OfficialWorkbook(sample_bytes)

    rows = [
        {
            'A': 'Gamer Cotton T-Shirt Stonewashed',
            'C': 'Novo',
            'D': 'Preto',
            'G': 'M',
            'O': 59.90,
            'N': 15,
            'R': 'Comfortable 100% cotton t-shirt for daily wear.'
        },
        {
            'A': 'Gamer Cotton T-Shirt Stonewashed',
            'C': 'Novo',
            'D': 'Preto',
            'G': 'G',
            'O': 59.90,
            'N': 20,
            'R': 'Comfortable 100% cotton t-shirt for daily wear.'
        }
    ]

    output_bytes = wb.export(rows)
    assert len(output_bytes) > 0

    with zipfile.ZipFile(io.BytesIO(output_bytes)) as z:
        assert 'xl/worksheets/sheet3.xml' in z.namelist()
        sheet_xml = z.read('xl/worksheets/sheet3.xml').decode('utf8')
        assert 'Gamer Cotton T-Shirt Stonewashed' in sheet_xml
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
    rows = [
        {
            'A': 'Tech Dev Developer T-Shirt',
            'C': 'Novo',
            'D': 'Branco',
            'G': 'G',
            'O': 49.90,
            'N': 10
        }
    ]

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
        assert 'Tech Dev Developer T-Shirt' in sheet_xml
