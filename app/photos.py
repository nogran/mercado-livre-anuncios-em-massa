"""Picture upload helper for Mercado Libre's official pictures API.

Uploads local image assets to generate the HTTPS secure URLs required
by the bulk listing spreadsheet's picture column.
"""
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from urllib.parse import urlsplit
import requests

from .exceptions import MarketplaceCredentialError, MarketplaceUploadError

UPLOAD_URL = 'https://api.mercadolibre.com/pictures/items/upload'
MAX_RESPONSE = 1024 * 1024
MIME_EXTENSIONS = {
    'image/png': 'png',
    'image/jpeg': 'jpg',
    'image/webp': 'webp'
}
_ml_transport = None


def bind_ml_transport(transport):
    """Install the Mercado Libre caller used when upload_picture receives none.

    Tests bind a fake for the in-process client. Production leaves this unset.
    """
    global _ml_transport
    previous = _ml_transport
    _ml_transport = transport
    return previous


def _https_url(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parts = urlsplit(value.strip())
    return parts.scheme == 'https' and bool(parts.hostname)


def _clean_error_detail(body: bytes) -> str:
    """Extract error messages from Mercado Libre response, masking any credentials."""
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return ''
    if not isinstance(payload, dict):
        return ''

    values = [payload.get('message'), payload.get('error')]
    causes = payload.get('cause')
    if isinstance(causes, list):
        values.extend(item.get('message') for item in causes if isinstance(item, dict))

    detail = ' — '.join(
        dict.fromkeys(
            str(v).strip() for v in values if isinstance(v, str) and v.strip()
        )
    )
    if not detail:
        return ''

    if re.search(r'(?i)(?:APP_USR|TG)-|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|bearer\s', detail):
        return ' Remote details were masked for security reasons.'
    return f' Detail: {detail[:240]}'


def upload_picture(
    token: str,
    raw: bytes,
    mime: str = 'image/jpeg',
    transport=None
) -> Dict[str, Any]:
    """Upload an image file to Mercado Libre pictures API and return the secure HTTPS URL."""
    token = token.strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()

    if not token:
        raise MarketplaceCredentialError('Mercado Libre access token is required.', 'ML_TOKEN_MISSING')

    if mime not in MIME_EXTENSIONS:
        raise MarketplaceUploadError(
            f'Unsupported image format: {mime}. Supported formats: PNG, JPEG, WEBP.',
            'INVALID_MIME'
        )

    extension = MIME_EXTENSIONS[mime]

    try:
        requester = transport or _ml_transport or requests.post
        response = requester(
            UPLOAD_URL,
            headers={'Authorization': f'Bearer {token}'},
            files={'file': (f'photo.{extension}', raw, mime)},
            timeout=(10, 60),
            allow_redirects=False,
            stream=True
        )

        body = bytearray()
        for chunk in response.iter_content(65536):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE:
                raise MarketplaceUploadError('Response from Mercado Libre exceeded maximum size limit.', 'RESPONSE_TOO_LARGE')
        body_bytes = bytes(body)

        if response.status_code == 401:
            raise MarketplaceCredentialError(
                'Mercado Libre access token rejected or expired (HTTP 401). Please renew the token.',
                'ML_TOKEN_INVALID'
            )
        if response.status_code == 403:
            detail = _clean_error_detail(body_bytes)
            raise MarketplaceCredentialError(
                f'Picture upload access denied (HTTP 403).{detail}',
                'ML_TOKEN_FORBIDDEN'
            )
        if response.status_code not in (200, 201):
            detail = _clean_error_detail(body_bytes)
            raise MarketplaceUploadError(
                f'Mercado Libre rejected the picture (HTTP {response.status_code}).{detail}',
                'UPLOAD_REJECTED'
            )

        payload = json.loads(body_bytes)
        options = payload.get('variations') or []
        picture = next(
            (
                item for item in options
                if isinstance(item, dict) and item.get('size') == payload.get('max_size')
            ),
            None,
        )
        if picture is None or not _https_url(picture.get('secure_url')):
            raise MarketplaceUploadError(
                'Mercado Libre did not return a secure HTTPS URL for the original image.',
                'MISSING_SECURE_URL',
            )

        now_utc = datetime.now(timezone.utc)
        expires_utc = now_utc + timedelta(days=2)

        return {
            'picture_id': payload.get('id'),
            'url': picture['secure_url'],
            'obtained_at': now_utc.isoformat(),
            'expires_at': expires_utc.isoformat()
        }

    except (MarketplaceCredentialError, MarketplaceUploadError):
        raise
    except requests.RequestException as e:
        raise MarketplaceUploadError(f'Network error while connecting to Mercado Libre: {str(e)}', 'NETWORK_ERROR') from e
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise MarketplaceUploadError(f'Unexpected response payload from Mercado Libre: {str(e)}', 'INVALID_RESPONSE') from e
