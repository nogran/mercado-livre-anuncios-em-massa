"""POST /v1/listings/build without network: a fake Mercado Libre counts uploads."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from app import build as build_module
from app.photos import bind_ml_transport
from app.server import app
from app.workbook import OfficialWorkbook
from tests.test_workbook import LOCAL_SAMPLE, completed_row

XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
JPEG = b'\xff\xd8\xff fake jpeg'
PNG = b'\x89PNG fake png'


class FakeMercadoLibre:
    """Answers each upload with a URL numbered by call; can fail on call N."""

    def __init__(self, fail_on=None, status=500):
        self.calls = []
        self.fail_on = fail_on
        self.status = status

    def __call__(self, url, headers=None, files=None, **kwargs):
        self.calls.append(files['file'][1])
        number = len(self.calls)
        status = self.status if number == self.fail_on else 201
        payload = {
            'id': f'PIC{number}',
            'max_size': '1200x1200',
            'variations': [{'size': '1200x1200', 'secure_url': f'https://http2.mlstatic.com/pic{number}.jpg'}],
        }

        class Response:
            status_code = status

            def iter_content(self, size):
                yield json.dumps(payload if status == 201 else {'message': 'server error'}).encode()

        return Response()


@pytest.fixture
def template():
    return LOCAL_SAMPLE.read_bytes()


@pytest.fixture
def wb(template):
    return OfficialWorkbook(template)


@pytest.fixture
def ml():
    fake = FakeMercadoLibre()
    previous = bind_ml_transport(fake)
    yield fake
    bind_ml_transport(previous)


def photo_row(wb, photos, **overrides):
    row = completed_row(wb, **overrides)
    row.pop('K', None)
    row['photos'] = photos
    return row


def post(template, rows, images=(), token='Bearer fixture-token'):
    files = [('template', ('template.xlsx', template, XLSX))]
    files += [('images', (name, raw, mime)) for name, raw, mime in images]
    headers = {'Authorization': token} if token else {}
    return TestClient(app).post('/v1/listings/build', files=files,
                                data={'payload': json.dumps({'rows': rows})}, headers=headers)


def sheet_cell(raw, wb, ref):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        sheet = archive.read(wb.profile.sheet_part).decode('utf8')
    cell = re.search(rf'<c\b[^>]*\br="{ref}"[^>]*(?:/>|>.*?</c>)', sheet, re.S)
    return cell.group(0) if cell else ''


IMAGES = [('frente.jpg', JPEG, 'image/jpeg'), ('costas.png', PNG, 'image/png')]


def test_build_uploads_each_image_once_and_writes_k_in_order(template, wb, ml):
    rows = [
        photo_row(wb, ['frente.jpg', 'costas.png'], G='M'),
        photo_row(wb, ['costas.png'], G='G'),
    ]
    response = post(template, rows, IMAGES)
    assert response.status_code == 200, response.text
    assert response.headers['content-type'] == XLSX
    assert response.headers['x-uploaded-photos'] == '2'
    assert ml.calls == [JPEG, PNG]
    first = sheet_cell(response.content, wb, 'K8')
    second = sheet_cell(response.content, wb, 'K9')
    assert 'https://http2.mlstatic.com/pic1.jpg,https://http2.mlstatic.com/pic2.jpg' in first
    assert 'https://http2.mlstatic.com/pic2.jpg' in second and 'pic1' not in second
    assert b'fixture-token' not in response.content


def test_rows_with_k_and_no_images_need_no_token(template, wb, ml):
    rows = [completed_row(wb, K='https://http2.mlstatic.com/ready.jpg')]
    response = post(template, rows, token=None)
    assert response.status_code == 200, response.text
    assert response.headers['x-uploaded-photos'] == '0'
    assert ml.calls == []
    assert 'ready.jpg' in sheet_cell(response.content, wb, 'K8')


def test_invalid_rows_upload_nothing_and_list_every_error(template, wb, ml):
    rows = [
        photo_row(wb, ['frente.jpg'], O=5),
        photo_row(wb, ['nao-enviada.jpg']),
        dict(photo_row(wb, ['frente.jpg']), K='https://http2.mlstatic.com/x.jpg'),
    ]
    response = post(template, rows, IMAGES)
    assert response.status_code == 422
    assert ml.calls == []
    errors = response.json()['errors']
    assert errors == [
        {'row': 1, 'column': 'O',
         'message': 'Listing row 1, column O must be between 8 and 1000000 with at most two decimals.'},
        {'row': 2, 'column': 'K', 'message': 'Listing row 2, column K: photo 1 was not sent in images.'},
        {'row': 3, 'column': 'K', 'message': 'Listing row 3, column K: send either K or photos, not both.'},
    ]
    assert 'nao-enviada' not in response.text


@pytest.mark.parametrize('photos, message', [
    ([], 'photos must be a non-empty list of image file names'),
    ('frente.jpg', 'photos must be a non-empty list of image file names'),
    (['frente.jpg', 'frente.jpg'], 'photos lists the same image twice'),
])
def test_photo_list_shape(template, wb, ml, photos, message):
    response = post(template, [photo_row(wb, photos)], IMAGES)
    assert response.status_code == 422
    assert response.json()['errors'] == [
        {'row': 1, 'column': 'K', 'message': f'Listing row 1, column K: {message}.'}]
    assert ml.calls == []


def test_missing_k_without_photos_is_still_required(template, wb, ml):
    row = completed_row(wb)
    row.pop('K')
    response = post(template, [row])
    assert response.status_code == 422
    assert response.json()['detail'] == 'Listing row 1, column K is required.'


def test_photos_need_a_token_before_any_upload(template, wb, ml):
    response = post(template, [photo_row(wb, ['frente.jpg'])], IMAGES, token=None)
    assert response.status_code == 401
    assert response.json()['code'] == 'ML_TOKEN_MISSING'
    assert ml.calls == []


def test_failure_mid_upload_returns_502_without_workbook(template, wb):
    fake = FakeMercadoLibre(fail_on=2)
    previous = bind_ml_transport(fake)
    try:
        response = post(template, [photo_row(wb, ['frente.jpg', 'costas.png'])], IMAGES)
    finally:
        bind_ml_transport(previous)
    assert response.status_code == 502
    body = response.json()
    assert body['code'] == 'UPLOAD_REJECTED'
    assert body['uploaded'] == 1
    assert 'costas.png' in body['detail']
    assert 'No workbook was written' in body['detail']
    assert 'fixture-token' not in response.text
    assert len(fake.calls) == 2


def test_rejected_token_answers_401(template, wb):
    fake = FakeMercadoLibre(fail_on=1, status=401)
    previous = bind_ml_transport(fake)
    try:
        response = post(template, [photo_row(wb, ['frente.jpg'])], IMAGES)
    finally:
        bind_ml_transport(previous)
    assert response.status_code == 401
    assert response.json()['code'] == 'ML_TOKEN_INVALID'


def test_image_limits_refuse_before_reading_rows(template, wb, ml, monkeypatch):
    rows = [photo_row(wb, ['frente.jpg'])]
    gif = post(template, rows, [('frente.jpg', b'GIF89a', 'image/gif')])
    assert gif.status_code == 415
    assert gif.json()['code'] == 'UNSUPPORTED_IMAGE'

    twice = post(template, rows, [IMAGES[0], IMAGES[0]])
    assert twice.status_code == 400
    assert twice.json()['code'] == 'DUPLICATE_IMAGE_NAME'

    monkeypatch.setattr(build_module, 'MAX_IMAGE', 8)
    big = post(template, rows, [IMAGES[0]])
    assert big.status_code == 413
    assert big.json()['code'] == 'IMAGE_TOO_LARGE'

    monkeypatch.setattr(build_module, 'MAX_IMAGES', 1)
    many = post(template, rows, IMAGES)
    assert many.status_code == 413
    assert many.json()['code'] == 'TOO_MANY_IMAGES'
    assert ml.calls == []


def test_unused_images_are_not_uploaded(template, wb, ml):
    response = post(template, [photo_row(wb, ['costas.png'])], IMAGES)
    assert response.status_code == 200, response.text
    assert ml.calls == [PNG]


def test_bad_payload_is_400(template):
    response = TestClient(app).post(
        '/v1/listings/build', files={'template': ('template.xlsx', template, XLSX)}, data={'payload': '{'})
    assert response.status_code == 400
