"""Original rendition only: max_size and an HTTPS URL. No smaller fallback."""
import json

import pytest
from fastapi.testclient import TestClient

from app.exceptions import MarketplaceUploadError
from app.photos import bind_ml_transport, upload_picture
from app.server import app


def _response(payload, status=201):
    class Response:
        status_code = status

        def iter_content(self, size):
            yield json.dumps(payload).encode()

    return Response()


def _payload(url='https://http2.mlstatic.com/original.jpg', size='1200x1200'):
    return {
        'id': 'PIC1',
        'max_size': '1200x1200',
        'variations': [
            {'size': '100x100', 'secure_url': 'https://http2.mlstatic.com/small.jpg'},
            {'size': size, 'secure_url': url},
        ],
    }


def test_upload_returns_only_the_max_size_https_url():
    result = upload_picture('token', b'\xff\xd8\xff', 'image/jpeg', lambda *args, **kwargs: _response(_payload()))
    assert result['picture_id'] == 'PIC1'
    assert result['url'] == 'https://http2.mlstatic.com/original.jpg'
    assert result['obtained_at']
    assert result['expires_at']


def test_upload_does_not_fall_back_to_another_secure_url():
    with pytest.raises(MarketplaceUploadError) as error:
        upload_picture('token', b'\xff\xd8\xff', 'image/jpeg', lambda *args, **kwargs: _response(
            _payload('http://http2.mlstatic.com/original.jpg')))
    assert error.value.code == 'MISSING_SECURE_URL'
    assert 'http://' not in str(error.value)


def test_endpoint_returns_the_same_fields_without_calling_the_network():
    previous = bind_ml_transport(lambda *args, **kwargs: _response(_payload()))
    try:
        response = TestClient(app).post(
            '/v1/photos/upload',
            headers={'Authorization': 'Bearer fixture-token'},
            files={'image': ('photo.jpg', b'\xff\xd8\xff', 'image/jpeg')},
        )
    finally:
        bind_ml_transport(previous)
    assert response.status_code == 200
    body = response.json()
    assert body['picture_id'] == 'PIC1'
    assert body['url'] == 'https://http2.mlstatic.com/original.jpg'
    assert body['obtained_at']
    assert body['expires_at']
    assert 'fixture-token' not in response.text
