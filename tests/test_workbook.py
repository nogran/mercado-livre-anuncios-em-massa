"""Unit tests for the OfficialWorkbook engine and the FastAPI server endpoints."""
import io
import json
import zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.workbook import OfficialWorkbook
from app.server import app

LOCAL_SAMPLE = Path(__file__).resolve().parents[2] / 'nogran-ml-excel' / 'Anunciar-09-06-10_43_36.xlsx'


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
