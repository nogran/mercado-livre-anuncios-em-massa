"""After Mercado Libre creates the listings from the workbook: find them and finish them.

Mercado Libre publishes the bulk workbook without free shipping (even when the
workbook asks for it) and leaves optional attributes empty. These helpers find
the items a workbook created, by family title, and fix what the seller asks:

- ``find_items``: the seller's items whose family title is one of the titles.
- ``free_shipping``: turn on free shipping, skipping items already on.
- ``fill_attributes``: write attributes only where the item has no value.
- ``video_links``: one video upload link per family.

Every change runs as a preview unless ``apply`` is true, never touches price,
stock or title, and repeating it is safe: an item already right is skipped.
Calls go one item at a time (Mercado Libre turned off ``/items?ids=``), in a
few threads with a reused connection.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import quote

import requests

from .exceptions import MarketplaceCredentialError, MarketplaceError
from .photos import _clean_error_detail

API = 'https://api.mercadolibre.com'
WORKERS = 8
VIDEO_UPLOAD = ('https://www.mercadolivre.com.br/video/creator/upload?item_id={item}&item_syi={item}'
                '&user_product_id={product}&user_product_syi={product}&origin=syi_dropdown')
_transport = None
_local = threading.local()


def bind_items_transport(transport):
    """Install the caller used instead of HTTPS: ``transport(method, url, headers=, json=, timeout=)``.

    Tests bind a fake. Production leaves this unset and uses a pooled session.
    """
    global _transport
    previous = _transport
    _transport = transport
    return previous


def _session() -> requests.Session:
    if not hasattr(_local, 'session'):
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        session = requests.Session()
        session.mount('https://', HTTPAdapter(max_retries=Retry(
            total=6, connect=6, read=2, status=4, backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504], allowed_methods=['GET', 'PUT'])))
        _local.session = session
    return _local.session


class ItemError(Exception):
    """One item could not be read or changed; the others go on."""


class MercadoLibre:
    """Seller API calls with one token. A rejected token stops everything; other errors are per item."""

    def __init__(self, token: str):
        token = (token or '').strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        if not token:
            raise MarketplaceCredentialError('Mercado Libre access token is required.', 'ML_TOKEN_MISSING')
        self._headers = {'Authorization': f'Bearer {token}'}

    def call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        caller = _transport or _session().request
        try:
            response = caller(method, API + path, headers=self._headers, json=body, timeout=(5, 30))
        except requests.RequestException as error:
            raise ItemError(f'Network error: {type(error).__name__}.') from error
        status = response.status_code
        if status == 401:
            raise MarketplaceCredentialError(
                'Mercado Libre access token rejected or expired (HTTP 401). Please renew the token.',
                'ML_TOKEN_INVALID')
        if status >= 400:
            raise ItemError(f'HTTP {status}.{_clean_error_detail(response.content)}')
        try:
            return response.json() if response.content else {}
        except ValueError as error:
            raise ItemError('Mercado Libre answered with something that is not JSON.') from error

    def item(self, item_id: str, attributes: str) -> Dict[str, Any]:
        return self.call('GET', f'/items/{quote(item_id)}?attributes={attributes}')

    def each(self, work: Callable[[str], Dict[str, Any]], ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Run ``work`` per item in threads; an ItemError becomes ``{"id", "error"}`` for that item."""
        def safe(item_id: str) -> Dict[str, Any]:
            try:
                return work(item_id)
            except ItemError as error:
                return {'id': item_id, 'error': str(error)}
        ids = list(ids)
        with ThreadPoolExecutor(min(WORKERS, max(1, len(ids)))) as pool:
            return list(pool.map(safe, ids))


def clean_ids(item_ids: Iterable[Any]) -> List[str]:
    """Item ids without blanks or repeats, in the order given."""
    seen: List[str] = []
    for value in item_ids or []:
        text = str(value).strip().upper() if value is not None else ''
        if text and text not in seen:
            seen.append(text)
    if not seen:
        raise MarketplaceError('Pass at least one item id (e.g. "MLB1234567890").', 'ITEMS_MISSING')
    return seen


def _split(results: List[Dict[str, Any]]) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {'changed': [], 'would_change': [], 'already': [], 'failed': []}
    for result in results:
        out[result.pop('state')].append(result if result.keys() - {'id'} else result['id'])
    return out


def default_terms(titles: List[str]) -> List[str]:
    """Search words when none are given: the first two words of each title, without repeats."""
    terms: List[str] = []
    for title in titles:
        words = ' '.join(title.split()[:2])
        if words and words.casefold() not in {t.casefold() for t in terms}:
            terms.append(words)
    return terms


def find_items(ml: MercadoLibre, titles: List[str], terms: Optional[List[str]] = None) -> Dict[str, Any]:
    """The seller's items whose family title matches one of ``titles`` (ignoring case).

    Mercado Libre changes the case of titles ("XP" becomes "Xp"), so the match ignores it.
    The search uses ``scan`` with ``scroll_id``, which goes past the 1,000 results that
    ``offset`` allows.
    """
    wanted = {t.strip().casefold(): t.strip() for t in titles if isinstance(t, str) and t.strip()}
    if not wanted:
        raise MarketplaceError('Pass at least one listing title.', 'TITLES_MISSING')
    terms = [t for t in (terms or default_terms(list(wanted.values()))) if t.strip()]
    seller = ml.call('GET', '/users/me').get('id')
    found: List[str] = []
    for term in terms:
        url = f'/users/{seller}/items/search?q={quote(term)}&search_type=scan&limit=100'
        page = ml.call('GET', url)
        while page.get('results'):
            found.extend(i for i in page['results'] if i not in found)
            page = ml.call('GET', f"{url}&scroll_id={quote(str(page.get('scroll_id', '')))}")

    def read(item_id: str) -> Dict[str, Any]:
        item = ml.item(item_id, 'id,family_name,status')
        return {'id': item_id, 'family_name': item.get('family_name') or '', 'status': item.get('status')}

    read_back = ml.each(read, sorted(found))
    failed = [r for r in read_back if 'error' in r]
    mine = [r for r in read_back if 'error' not in r and r['family_name'].casefold() in wanted]
    per_title = {title: 0 for title in wanted.values()}
    for item in mine:
        per_title[wanted[item['family_name'].casefold()]] += 1
    return {
        'items': mine,
        'item_ids': [r['id'] for r in mine],
        'per_title': per_title,
        'missing_titles': [t for t, n in per_title.items() if not n],
        'searched': terms,
        'scanned': len(found),
        'failed': failed,
    }


def free_shipping(ml: MercadoLibre, item_ids: List[str], apply: bool) -> Dict[str, Any]:
    """Turn on free shipping where it is off; with ``apply`` the change is read back to confirm."""
    def one(item_id: str) -> Dict[str, Any]:
        if ml.item(item_id, 'id,shipping').get('shipping', {}).get('free_shipping'):
            return {'id': item_id, 'state': 'already'}
        if not apply:
            return {'id': item_id, 'state': 'would_change'}
        ml.call('PUT', f'/items/{quote(item_id)}', {'shipping': {'free_shipping': True}})
        if not ml.item(item_id, 'id,shipping').get('shipping', {}).get('free_shipping'):
            raise ItemError('Mercado Libre accepted the change but the item still has no free shipping.')
        return {'id': item_id, 'state': 'changed'}
    return _finish(ml.each(lambda i: _state(one, i), item_ids), apply)


def fill_attributes(ml: MercadoLibre, item_ids: List[str], attributes: Dict[str, str],
                    apply: bool) -> Dict[str, Any]:
    """Write each attribute only where the item has no value; a filled attribute is never replaced."""
    wanted = {str(k).strip().upper(): str(v).strip() for k, v in (attributes or {}).items()
              if str(k).strip() and v is not None and str(v).strip()}
    if not wanted:
        raise MarketplaceError('Pass attributes as {"ATTRIBUTE_ID": "value"}.', 'ATTRIBUTES_MISSING')

    def empty(item_id: str) -> Dict[str, str]:
        item = ml.item(item_id, 'id,attributes')
        filled = {a.get('id') for a in item.get('attributes') or []
                  if isinstance(a, dict) and (a.get('value_name') or a.get('value_id'))}
        return {k: v for k, v in wanted.items() if k not in filled}

    def one(item_id: str) -> Dict[str, Any]:
        missing = empty(item_id)
        if not missing:
            return {'id': item_id, 'state': 'already'}
        if not apply:
            return {'id': item_id, 'state': 'would_change', 'attributes': sorted(missing)}
        ml.call('PUT', f'/items/{quote(item_id)}',
                {'attributes': [{'id': k, 'value_name': v} for k, v in missing.items()]})
        still = empty(item_id)
        if still:
            raise ItemError(f'Still empty after the change: {", ".join(sorted(still))}.')
        return {'id': item_id, 'state': 'changed', 'attributes': sorted(missing)}
    return _finish(ml.each(lambda i: _state(one, i), item_ids), apply)


def video_links(ml: MercadoLibre, item_ids: List[str]) -> Dict[str, Any]:
    """One video upload link per family title (the lowest item id of the family)."""
    def read(item_id: str) -> Dict[str, Any]:
        item = ml.item(item_id, 'id,family_name,user_product_id')
        return {'id': item_id, 'family_name': item.get('family_name') or item_id,
                'user_product_id': item.get('user_product_id')}

    read_back = ml.each(read, sorted(item_ids))
    links: Dict[str, Dict[str, str]] = {}
    for item in read_back:
        if 'error' in item or not item['user_product_id'] or item['family_name'] in links:
            continue
        links[item['family_name']] = {
            'item_id': item['id'],
            'url': VIDEO_UPLOAD.format(item=quote(item['id']), product=quote(str(item['user_product_id']))),
        }
    return {
        'links': [{'family_name': name, **link} for name, link in links.items()],
        'failed': [r for r in read_back if 'error' in r],
    }


def _state(work: Callable[[str], Dict[str, Any]], item_id: str) -> Dict[str, Any]:
    try:
        return work(item_id)
    except ItemError as error:
        return {'id': item_id, 'state': 'failed', 'error': str(error)}


def _finish(results: List[Dict[str, Any]], apply: bool) -> Dict[str, Any]:
    for result in results:  # an error raised by each() itself has no state yet
        result.setdefault('state', 'failed')
    split = _split(results)
    return {'applied': apply, 'total': len(results), **split,
            'counts': {key: len(value) for key, value in split.items()}}
