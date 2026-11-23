"""MCP tools for after publishing, against a fake Mercado Libre seller API."""
import copy
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from mcp import Client

from app.items import bind_items_transport
from app.mcp_server import server

pytestmark = pytest.mark.anyio
TOKEN = 'APP_USR-items-token-456'


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def item(item_id, family, free=False, attributes=(), product='UP1', status='active'):
    return {'id': item_id, 'family_name': family, 'status': status, 'user_product_id': product,
            'shipping': {'free_shipping': free, 'mode': 'me2'},
            'attributes': [{'id': k, 'value_name': v} for k, v in attributes]}


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.content = json.dumps(body).encode()

    def json(self):
        return json.loads(self.content)


class FakeSeller:
    """Seller 99 with a few items; search pages of two results through scroll_id."""

    def __init__(self, items):
        self.items = {i['id']: copy.deepcopy(i) for i in items}
        self.calls = []
        self.status = {}

    def __call__(self, method, url, headers=None, json=None, timeout=None):
        assert headers['Authorization'] == f'Bearer {TOKEN}'
        parts = urlsplit(url)
        query = parse_qs(parts.query)
        self.calls.append((method, parts.path, json))
        status = self.status.get(parts.path, 200)
        if status != 200:
            return Response(status, {'message': 'not found'})
        if parts.path == '/users/me':
            return Response(200, {'id': 99})
        if parts.path == '/users/99/items/search':
            term = query['q'][0].casefold()
            hits = sorted(i for i, it in self.items.items() if term in it['family_name'].casefold())
            start = int(query.get('scroll_id', ['0'])[0] or 0)
            return Response(200, {'results': hits[start:start + 2], 'scroll_id': str(start + 2)})
        target = self.items[parts.path.split('/')[2]]
        if parts.path.endswith('/visits/time_window'):
            days = int(query['last'][0])
            today = datetime.now(timezone.utc).date()
            results = [{'date': f'{today - timedelta(days=n)}T00:00:00Z', 'total': target.get('daily', 0)}
                       for n in range(days)]
            return Response(200, {'total_visits': sum(r['total'] for r in results), 'results': results})
        if method == 'PUT':
            if 'shipping' in json:
                target['shipping'].update(json['shipping'])
            target['attributes'].extend(json.get('attributes', []))
        return Response(200, copy.deepcopy(target))


ITEMS = [
    item('MLB1', 'Camiseta Infantil Dino Xp Preta'),
    item('MLB2', 'Camiseta Infantil Dino Xp Preta', free=True),
    item('MLB3', 'Camiseta Infantil Gato Branca', attributes=[('COMPOSITION', 'Poliéster')], product='UP3'),
    item('MLB4', 'Camiseta Infantil Outra Loja'),
]


@pytest.fixture
def seller(monkeypatch, tmp_path):
    fake = FakeSeller(ITEMS)
    previous = bind_items_transport(fake)
    monkeypatch.setenv('ML_ACCESS_TOKEN', TOKEN)
    monkeypatch.setenv('MLAM_WORKDIR', str(tmp_path))
    yield fake
    bind_items_transport(previous)


async def call(name, arguments):
    async with Client(server) as client:
        return await client.call_tool(name, arguments)


def puts(fake):
    return [c for c in fake.calls if c[0] == 'PUT']


async def test_find_by_rows_file_ignores_case_and_counts(seller, tmp_path):
    rows = [{'A': 'Camiseta Infantil Dino XP Preta'}, {'A': 'Camiseta Infantil Dino XP Preta'},
            {'A': 'Camiseta Infantil Gato Branca'}, {'A': 'Camiseta Infantil Sumiu'}]
    (tmp_path / 'linhas.json').write_text(json.dumps({'rows': rows}), encoding='utf8')
    result = await call('find_listings', {'rows_path': 'linhas.json'})
    assert not result.is_error, result.content
    body = result.structured_content
    assert body['item_ids'] == ['MLB1', 'MLB2', 'MLB3']
    assert body['searched'] == ['Camiseta Infantil']
    assert body['scanned'] == 4  # two pages through scroll_id
    assert body['missing_titles'] == ['Camiseta Infantil Sumiu']
    assert body['expected_items'] == 4 and body['count_matches'] is False
    assert puts(seller) == []


async def test_find_by_titles_with_search_terms(seller):
    result = await call('find_listings', {'titles': ['camiseta infantil gato branca'], 'search_terms': ['Gato']})
    body = result.structured_content
    assert body['item_ids'] == ['MLB3'] and body['searched'] == ['Gato']
    assert body['expected_items'] is None and body['count_matches'] is None


async def test_free_shipping_previews_then_applies(seller):
    result = await call('set_free_shipping', {'item_ids': ['MLB1', 'mlb2', 'MLB1', 'MLB3']})
    body = result.structured_content
    assert body['applied'] is False
    assert body['would_change'] == ['MLB1', 'MLB3'] and body['already'] == ['MLB2']
    assert puts(seller) == []

    result = await call('set_free_shipping', {'item_ids': ['MLB1', 'MLB2', 'MLB3'], 'apply': True})
    body = result.structured_content
    assert body['changed'] == ['MLB1', 'MLB3'] and body['counts']['already'] == 1
    assert [c[1] for c in puts(seller)] == ['/items/MLB1', '/items/MLB3']
    assert puts(seller)[0][2] == {'shipping': {'free_shipping': True}}

    again = await call('set_free_shipping', {'item_ids': ['MLB1', 'MLB2', 'MLB3'], 'apply': True})
    assert again.structured_content['already'] == ['MLB1', 'MLB2', 'MLB3']


async def test_attributes_fill_only_empty_values(seller):
    wanted = {'COMPOSITION': 'Algodão', 'SLEEVE_TYPE': 'Curta'}
    result = await call('fill_attributes', {'item_ids': ['MLB1', 'MLB3'], 'attributes': wanted})
    body = result.structured_content
    assert body['would_change'] == [{'id': 'MLB1', 'attributes': ['COMPOSITION', 'SLEEVE_TYPE']},
                                    {'id': 'MLB3', 'attributes': ['SLEEVE_TYPE']}]
    assert puts(seller) == []

    result = await call('fill_attributes', {'item_ids': ['MLB1', 'MLB3'], 'attributes': wanted, 'apply': True})
    assert result.structured_content['counts']['changed'] == 2
    sent = {c[1]: c[2] for c in puts(seller)}
    assert sent['/items/MLB3'] == {'attributes': [{'id': 'SLEEVE_TYPE', 'value_name': 'Curta'}]}
    composition = [a for a in seller.items['MLB3']['attributes'] if a['id'] == 'COMPOSITION']
    assert composition == [{'id': 'COMPOSITION', 'value_name': 'Poliéster'}]


async def test_video_links_one_per_family(seller):
    result = await call('video_upload_links', {'item_ids': ['MLB2', 'MLB1', 'MLB3']})
    links = result.structured_content['links']
    assert [(link['family_name'], link['item_id']) for link in links] == [
        ('Camiseta Infantil Dino Xp Preta', 'MLB1'), ('Camiseta Infantil Gato Branca', 'MLB3')]
    assert 'item_id=MLB3' in links[1]['url'] and 'user_product_id=UP3' in links[1]['url']


async def test_one_unreadable_item_does_not_stop_the_others(seller):
    seller.status['/items/MLB3'] = 404
    result = await call('set_free_shipping', {'item_ids': ['MLB1', 'MLB3'], 'apply': True})
    body = result.structured_content
    assert body['changed'] == ['MLB1']
    assert body['failed'][0]['id'] == 'MLB3' and 'HTTP 404' in body['failed'][0]['error']


async def test_missing_and_rejected_token(seller, monkeypatch):
    monkeypatch.delenv('ML_ACCESS_TOKEN')
    result = await call('set_free_shipping', {'item_ids': ['MLB1']})
    assert result.is_error and result.structured_content['code'] == 'ML_TOKEN_MISSING'

    monkeypatch.setenv('ML_ACCESS_TOKEN', TOKEN)
    seller.status['/users/me'] = 401
    result = await call('find_listings', {'titles': ['Camiseta Infantil Gato Branca']})
    assert result.is_error and result.structured_content['code'] == 'ML_TOKEN_INVALID'
    assert TOKEN not in result.content[0].text


@pytest.mark.parametrize('name, arguments, code', [
    ('set_free_shipping', {'item_ids': [' ', '']}, 'ITEMS_MISSING'),
    ('fill_attributes', {'item_ids': ['MLB1'], 'attributes': {}}, 'ATTRIBUTES_MISSING'),
    ('find_listings', {}, 'TITLES_MISSING'),
    ('find_listings', {'titles': ['x'], 'rows_path': 'linhas.json'}, 'ROWS_AMBIGUOUS'),
])
async def test_refusals(seller, name, arguments, code):
    result = await call(name, arguments)
    assert result.is_error and result.structured_content['code'] == code, result.content
    assert puts(seller) == []


async def test_stats_per_item_and_family(seller):
    ten_days_ago = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    for item_id, daily, sold, status in (('MLB1', 2, 1, 'active'), ('MLB2', 1, 0, 'paused'), ('MLB3', 5, 3, 'active')):
        seller.items[item_id].update(daily=daily, sold_quantity=sold, status=status, date_created=ten_days_ago)
    result = await call('listing_stats', {'item_ids': ['MLB1', 'MLB2', 'MLB3'], 'days': 14})
    assert not result.is_error, result.content
    body = result.structured_content
    first = body['items'][0]
    assert first == {'id': 'MLB1', 'family_name': 'Camiseta Infantil Dino Xp Preta', 'status': 'active',
                     'sold': 1, 'visits': 28, 'visits_7d': 14, 'age_days': 10}
    assert [(f['family_name'], f['items'], f['active'], f['visits'], f['sold']) for f in body['families']] == [
        ('Camiseta Infantil Gato Branca', 1, 1, 70, 3), ('Camiseta Infantil Dino Xp Preta', 2, 1, 42, 1)]
    assert puts(seller) == []


async def test_stats_refuses_a_bad_window(seller):
    result = await call('listing_stats', {'item_ids': ['MLB1'], 'days': 91})
    assert result.is_error and result.structured_content['code'] == 'DAYS_INVALID'
