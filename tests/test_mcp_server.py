"""MCP tools through an in-memory client: same core and same answers as the HTTP server."""
import json
import re
import shutil

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from app.mcp_server import server
from app.photos import bind_ml_transport
from app.server import app
from app.validation import row_errors
from app.workbook import OfficialWorkbook, digest
from tests.test_build import JPEG, FakeMercadoLibre
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
                cells[ref[1] + ref[2]] = cell
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


TOKEN = 'APP_USR-fixture-token-123'


@pytest.fixture
def ml(monkeypatch):
    fake = FakeMercadoLibre()
    previous = bind_ml_transport(fake)
    monkeypatch.setenv('ML_ACCESS_TOKEN', TOKEN)
    yield fake
    bind_ml_transport(previous)


def without_k(wb, **overrides):
    row = completed_row(wb, **overrides)
    row.pop('K', None)
    return row


async def test_build_gate_same_cells_as_http_build(work, ml):
    rows = json.loads((EXAMPLES / 'build-camiseta.json').read_text(encoding='utf8'))['rows']
    (work / 'frente.jpg').write_bytes(JPEG)
    (work / 'costas.jpg').write_bytes(JPEG + b'2')
    images = [('images', ('frente.jpg', JPEG, 'image/jpeg')), ('images', ('costas.jpg', JPEG + b'2', 'image/jpeg'))]
    http = TestClient(app).post(
        '/v1/listings/build',
        files=[('template', ('modelo.xlsx', LOCAL_SAMPLE.read_bytes(), XLSX))] + images,
        data={'payload': json.dumps({'rows': rows})},
        headers={'Authorization': f'Bearer {TOKEN}'},
    )
    assert http.status_code == 200, http.text
    http_calls = list(ml.calls)
    ml.calls.clear()

    result = await call('build_listings', {'template_path': 'modelo.xlsx', 'rows': rows, 'output_path': 'saida.xlsx'})
    assert not result.is_error, result.content
    written = (work / 'saida.xlsx').read_bytes()
    assert result.structured_content == {
        'output_path': 'saida.xlsx', 'rows': 2, 'uploaded': 2, 'sha256': digest(written)}
    assert ml.calls == http_calls == [JPEG, JPEG + b'2']
    cells = editable_cells(written)
    assert cells == editable_cells(http.content)
    assert 'pic1.jpg,https://http2.mlstatic.com/pic2.jpg' in cells['K8']
    assert TOKEN.encode() not in written


async def test_build_checks_everything_before_the_first_upload(work, wb, ml):
    rows = [dict(without_k(wb, O=5), photos=['fotos/frente.jpg']), dict(without_k(wb), photos=['fotos/sumiu.jpg'])]
    result = await call('build_listings', {'template_path': 'modelo.xlsx', 'rows': rows, 'output_path': 'saida.xlsx'})
    assert result.is_error
    body = result.structured_content
    assert body['code'] == 'VALIDATION_ERROR'
    assert [(e['row'], e['column']) for e in body['errors']] == [(1, 'O'), (2, 'K')]
    assert ml.calls == []
    assert not (work / 'saida.xlsx').exists()


async def test_build_failure_midway_writes_nothing_and_hides_the_token(work, wb, monkeypatch):
    fake = FakeMercadoLibre(fail_on=2)
    previous = bind_ml_transport(fake)
    monkeypatch.setenv('ML_ACCESS_TOKEN', TOKEN)
    try:
        result = await call('build_listings', {
            'template_path': 'modelo.xlsx', 'output_path': 'saida.xlsx',
            'rows': [dict(without_k(wb), photos=['fotos/frente.jpg', 'fotos/costas.png'])]})
    finally:
        bind_ml_transport(previous)
    assert result.is_error
    body = result.structured_content
    assert body['code'] == 'PHOTO_UPLOAD_FAILED'
    assert body['uploaded'] == 1
    assert body['cause'] == 'UPLOAD_REJECTED'
    assert len(fake.calls) == 2
    assert not (work / 'saida.xlsx').exists()
    assert TOKEN not in json.dumps(body) and TOKEN not in result.content[0].text


async def test_photos_without_token_upload_nothing(work, wb):
    fake = FakeMercadoLibre()
    previous = bind_ml_transport(fake)
    try:
        built = await call('build_listings', {'template_path': 'modelo.xlsx', 'output_path': 'saida.xlsx',
                                              'rows': [dict(without_k(wb), photos=['fotos/frente.jpg'])]})
        single = await call('upload_photo', {'image_path': 'fotos/frente.jpg'})
    finally:
        bind_ml_transport(previous)
    for result in (built, single):
        assert result.is_error
        assert result.structured_content['code'] == 'ML_TOKEN_MISSING'
        assert 'ML_ACCESS_TOKEN' in result.structured_content['detail']
    assert fake.calls == []


async def test_rows_with_ready_urls_need_no_token(work, wb):
    result = await call('build_listings', {'template_path': 'modelo.xlsx', 'output_path': 'saida.xlsx',
                                           'rows': [completed_row(wb, K='https://http2.mlstatic.com/ok.jpg')]})
    assert not result.is_error, result.content
    assert result.structured_content['uploaded'] == 0


async def test_upload_photo_returns_the_url(work, ml):
    result = await call('upload_photo', {'image_path': 'fotos/costas.png'})
    assert not result.is_error, result.content
    body = result.structured_content
    assert body['picture_id'] == 'PIC1'
    assert body['url'] == 'https://http2.mlstatic.com/pic1.jpg'
    assert ml.calls == [b'\x89PNG fake png']


async def test_rejected_token_is_reported_without_the_token(work, monkeypatch):
    fake = FakeMercadoLibre(fail_on=1, status=401)
    previous = bind_ml_transport(fake)
    monkeypatch.setenv('ML_ACCESS_TOKEN', TOKEN)
    try:
        result = await call('upload_photo', {'image_path': 'fotos/frente.jpg'})
    finally:
        bind_ml_transport(previous)
    assert result.is_error and result.structured_content['code'] == 'ML_TOKEN_INVALID'
    assert TOKEN not in result.content[0].text


async def test_no_tool_takes_a_token_argument():
    async with Client(server) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        'inspect_template', 'validate_rows', 'fill_workbook', 'upload_photo', 'build_listings',
        'find_listings', 'set_free_shipping', 'fill_attributes', 'video_upload_links'}
    for tool in tools:
        assert not any('token' in name.lower() or 'auth' in name.lower()
                       for name in tool.input_schema.get('properties', {}))


async def test_example_resource_has_the_columns_and_rows():
    async with Client(server) as client:
        listed = [str(r.uri) for r in (await client.list_resources()).resources]
        read = await client.read_resource('mlam://exemplos/camiseta')
    assert listed == ['mlam://exemplos/camiseta']
    text = read.contents[0].text
    assert '| K |' in text
    example = json.loads(text.split('```json\n', 1)[1].split('```', 1)[0])
    assert example == json.loads((EXAMPLES / 'build-camiseta.json').read_text(encoding='utf8'))


async def test_rows_path_gives_the_same_answers_as_inline_rows(work, wb):
    rows = [completed_row(wb), completed_row(wb, A='')]
    (work / 'linhas.json').write_text(json.dumps(rows), encoding='utf8')
    inline = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows': rows})
    by_file = await call('validate_rows', {'template_path': 'modelo.xlsx', 'rows_path': 'linhas.json'})
    assert by_file.structured_content == inline.structured_content
    assert by_file.structured_content['errors'] == row_errors(wb.profile, rows)

    good = [completed_row(wb)]
    (work / 'boas.json').write_text(json.dumps({'rows': good}), encoding='utf-8-sig')
    await call('fill_workbook', {'template_path': 'modelo.xlsx', 'rows': good, 'output_path': 'inline.xlsx'})
    result = await call('fill_workbook', {'template_path': 'modelo.xlsx', 'rows_path': 'boas.json',
                                          'output_path': 'arquivo.xlsx'})
    assert not result.is_error, result.content
    assert result.structured_content['rows'] == 1
    assert (work / 'arquivo.xlsx').read_bytes() == (work / 'inline.xlsx').read_bytes()


async def test_build_reads_the_example_file_as_rows_path(work, ml):
    shutil.copy(EXAMPLES / 'build-camiseta.json', work / 'build.json')
    (work / 'frente.jpg').write_bytes(JPEG)
    (work / 'costas.jpg').write_bytes(JPEG + b'2')
    result = await call('build_listings', {'template_path': 'modelo.xlsx', 'rows_path': 'build.json',
                                           'output_path': 'saida.xlsx'})
    assert not result.is_error, result.content
    assert result.structured_content['rows'] == 2 and result.structured_content['uploaded'] == 2
    assert ml.calls == [JPEG, JPEG + b'2']


@pytest.mark.parametrize('arguments, content, code', [
    ({}, None, 'ROWS_MISSING'),
    ({'rows': [], 'rows_path': 'linhas.json'}, '[]', 'ROWS_AMBIGUOUS'),
    ({'rows_path': 'linhas.json'}, '{"rows": ', 'ROWS_FILE_INVALID'),
    ({'rows_path': 'linhas.json'}, '{"linhas": []}', 'ROWS_FILE_INVALID'),
    ({'rows_path': 'sumiu.json'}, None, 'FILE_NOT_FOUND'),
    ({'rows_path': '../linhas.json'}, None, 'PATH_OUTSIDE_WORKDIR'),
])
async def test_rows_path_refusals(work, arguments, content, code):
    if content is not None:
        (work / 'linhas.json').write_text(content, encoding='utf8')
    for tool, extra in (('validate_rows', {}), ('fill_workbook', {'output_path': 'saida.xlsx'}),
                        ('build_listings', {'output_path': 'saida.xlsx'})):
        result = await call(tool, {'template_path': 'modelo.xlsx', **extra, **arguments})
        assert result.is_error and result.structured_content['code'] == code, (tool, result.content)
    assert not (work / 'saida.xlsx').exists()
