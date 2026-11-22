"""MCP tools through an in-memory client: same core and same answers as the HTTP server."""
import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.mcp_server import server
from app.server import app
from app.validation import row_errors
from app.workbook import OfficialWorkbook, digest
from tests.test_workbook import LOCAL_SAMPLE, completed_row

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def work(tmp_path, monkeypatch):
    shutil.copy(LOCAL_SAMPLE, tmp_path / 'modelo.xlsx')
    (tmp_path / 'fotos').mkdir()
    (tmp_path / 'fotos' / 'frente.jpg').write_bytes(b'\xff\xd8\xff fake jpeg')
    (tmp_path / 'fotos' / 'costas.png').write_bytes(b'\x89PNG fake png')
    monkeypatch.setenv('MLAM_WORKDIR', str(tmp_path))
    monkeypatch.delenv('ML_ACCESS_TOKEN', raising=False)
    return tmp_path


@pytest.fixture
def wb():
    return OfficialWorkbook(LOCAL_SAMPLE.read_bytes())


async def call(name, arguments):
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


async def test_tools_are_listed_with_hints():
    async with Client(server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert {'inspect_template', 'validate_rows'} <= set(tools)
    assert tools['inspect_template'].annotations.read_only_hint is True
    assert tools['validate_rows'].annotations.read_only_hint is True


async def test_inspect_summary_and_chosen_columns(work, wb):
    result = await call('inspect_template', {'template_path': 'modelo.xlsx'})
    assert not result.is_error
    body = result.structured_content
    assert body['sha256'] == wb.sha256
    assert body['rows_per_batch'] == wb.profile.last_row - wb.profile.first_row + 1
    assert 'A' in body['required']
    assert set(body['options']) <= set(wb.profile.strict)
    assert body['omitted_options']['D'] == len(wb.profile.options['D'])

    result = await call('inspect_template', {'template_path': 'modelo.xlsx', 'columns': ['d', 'G']})
    assert result.structured_content['options']['D'] == wb.profile.options['D']
    assert result.structured_content['options']['G'] == wb.profile.options['G']

    result = await call('inspect_template', {'template_path': 'modelo.xlsx', 'columns': ['ZZ']})
    assert result.is_error and result.structured_content['code'] == 'UNKNOWN_COLUMN'


async def test_template_outside_the_work_folder_is_refused(work):
    result = await call('inspect_template', {'template_path': str(LOCAL_SAMPLE)})
    assert result.is_error
    assert result.structured_content['code'] == 'PATH_OUTSIDE_WORKDIR'


async def test_validate_matches_row_errors(work, wb):
    good = completed_row(wb)
    bad = completed_row(wb, A='', N='muitos')
    result = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows': [good, bad]})
    assert not result.is_error
    assert result.structured_content == {'ok': False, 'errors': row_errors(wb.profile, [good, bad])}

    result = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows': [good]})
    assert result.structured_content == {'ok': True, 'errors': []}


async def test_validate_checks_photo_files_without_uploading(work, wb):
    row = completed_row(wb)
    row.pop('K', None)
    ok = dict(row, photos=['fotos/frente.jpg', 'fotos/costas.png'])
    missing = dict(row, photos=['fotos/nao-existe.jpg'])
    outside = dict(row, photos=['../fora.jpg'])
    gif = dict(row, photos=['fotos/anim.gif'])
    result = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows': [ok, missing, outside, gif]})
    body = result.structured_content
    assert body['ok'] is False
    assert [(e['row'], e['column']) for e in body['errors']] == [(2, 'K'), (3, 'K'), (4, 'K')]
    assert 'not found' in body['errors'][0]['message']
    assert 'inside the work folder' in body['errors'][1]['message']
    assert '.png, .jpg' in body['errors'][2]['message']


async def test_photos_and_k_together_is_a_row_error(work, wb):
    row = dict(completed_row(wb), K='https://http2.mlstatic.com/a.jpg', photos=['fotos/frente.jpg'])
    result = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows': [row]})
    assert result.structured_content['errors'][0]['message'].endswith('send either K or photos, not both.')


EXAMPLES = LOCAL_SAMPLE.parents[2] / 'examples'
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def editable_cells(raw):
    """Every editable cell in the data window, as {"K9": "<c ...>"}."""
    wb = OfficialWorkbook(raw)
    profile = wb.profile
    cells = {}
    for number, body in re.findall(r'<row\b[^>]*\br="(\d+)"[^>]*>(.*?)</row>', wb.xml, re.S):
        if not profile.first_row <= int(number) <= profile.last_row:
            continue
        for cell in re.findall(r'<c\b[^>]*(?:/>|>.*?</c>)', body, re.S):
            ref = re.search(r'\br="([A-Z]+)(\d+)"', cell)
            if ref[1] in profile.editable:
                cells[ref[0]] = cell
    return cells


async def test_fill_gate_same_cells_as_http_fill(work):
    rows = json.loads((EXAMPLES / 'fill-camiseta.json').read_text(encoding='utf8'))
    rows = rows['rows'] if isinstance(rows, dict) else rows
    http = TestClient(app).post(
        '/v1/workbooks/fill',
        files={'template': ('modelo.xlsx', LOCAL_SAMPLE.read_bytes(), XLSX)},
        data={'payload': json.dumps({'rows': rows})},
    )
    assert http.status_code == 200, http.text

    result = await call('fill_workbook', {'template_path': 'modelo.xlsx', 'rows': rows, 'output_path': 'saida.xlsx'})
    assert not result.is_error, result.content
    written = (work / 'saida.xlsx').read_bytes()
    assert result.structured_content == {'output_path': 'saida.xlsx', 'rows': len(rows), 'sha256': digest(written)}
    assert editable_cells(written) == editable_cells(http.content)
    assert any('Camiseta' in cell for cell in editable_cells(written).values())


async def test_fill_writes_nothing_on_invalid_rows(work, wb):
    rows = [completed_row(wb), completed_row(wb, A='')]
    result = await call('fill_workbook', {'template_path': 'modelo.xlsx', 'rows': rows, 'output_path': 'saida.xlsx'})
    assert result.is_error
    body = result.structured_content
    assert body['code'] == 'VALIDATION_ERROR'
    assert body['errors'] == row_errors(wb.profile, rows)
    assert not (work / 'saida.xlsx').exists()


async def test_fill_keeps_an_existing_file_unless_asked(work, wb):
    (work / 'saida.xlsx').write_bytes(b'old')
    arguments = {'template_path': 'modelo.xlsx', 'rows': [completed_row(wb)], 'output_path': 'saida.xlsx'}
    result = await call('fill_workbook', arguments)
    assert result.is_error and result.structured_content['code'] == 'OUTPUT_EXISTS'
    assert (work / 'saida.xlsx').read_bytes() == b'old'
    result = await call('fill_workbook', dict(arguments, overwrite=True))
    assert not result.is_error
    assert (work / 'saida.xlsx').read_bytes() != b'old'


async def test_fill_sends_photo_rows_to_build(work, wb):
    row = dict(completed_row(wb), photos=['fotos/frente.jpg'])
    result = await call('fill_workbook', {'template_path': 'modelo.xlsx', 'rows': [row], 'output_path': 'saida.xlsx'})
    assert result.is_error and result.structured_content['code'] == 'PHOTOS_NEED_BUILD'
