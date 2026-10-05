import ipaddress

import car
import car_service
import nav_intent

NETS = [ipaddress.ip_network('127.0.0.0/8'), ipaddress.ip_network('198.51.100.0/24')]  # TEST-NET-2


def test_auth_needs_both_network_and_bearer():
    ok = car_service.authorized
    assert ok('127.0.0.1', 'Bearer s3cr3t-value', 's3cr3t-value', NETS)
    assert ok('198.51.100.7', 'Bearer s3cr3t-value', 's3cr3t-value', NETS)
    assert not ok('203.0.113.9', 'Bearer s3cr3t-value', 's3cr3t-value', NETS)   # outside
    assert not ok('127.0.0.1', 'Bearer wrong', 's3cr3t-value', NETS)
    assert not ok('127.0.0.1', '', 's3cr3t-value', NETS)


def test_no_lock_or_trunk_commands_exposed():
    banned = ('lock', 'unlock', 'trunk', 'frunk', 'window', 'drive')
    assert not [n for n in car_service.ALLOWED if any(b in n for b in banned)]


def test_clean_query_strips_spoken_fillers():
    assert car_service.clean_query('pizza place on main street') == 'pizza place main street'
    assert car_service.clean_query('the closest coffee shop') == 'coffee shop'
    assert car_service.clean_query('pizza near me') == 'pizza'


def test_address_or_no_home_goes_straight_to_the_car(monkeypatch):
    called = []
    assert car_service.search_near_home('350 5th ave', fetch=called.append) == ('350 5th ave', '350 5th ave')
    assert car_service.search_near_home('museum', fetch=called.append) == ('museum', 'museum')  # no HOME_LAT
    assert called == []


def test_nearest_hit_wins(monkeypatch):
    monkeypatch.setenv('HOME_LAT', '40.7484')
    monkeypatch.setenv('HOME_LON', '-73.9857')
    hits = [
        {'lat': '40.90', 'lon': '-73.90', 'name': 'Far Cafe', 'address': {'road': 'A St', 'city': 'X'}},
        {'lat': '40.75', 'lon': '-73.98', 'name': 'Near Cafe',
         'address': {'house_number': '1', 'road': 'B St', 'city': 'New York', 'state': 'New York', 'postcode': '10001'}},
    ]
    label, addr = car_service.search_near_home('cafe near me', fetch=lambda url: hits)
    assert label == 'Near Cafe'
    assert addr == 'Near Cafe, 1 B St, New York, New York, 10001'


def test_no_hits_falls_back_to_raw_text(monkeypatch):
    monkeypatch.setenv('HOME_LAT', '40.7484')
    monkeypatch.setenv('HOME_LON', '-73.9857')
    assert car_service.search_near_home('tiny ice cream shop', fetch=lambda url: []) == \
        ('tiny ice cream shop', 'tiny ice cream shop')


def test_nav_payload_shape():
    p = car_service.nav_payload('Central Park', now=1.5)
    assert p == {'type': 'share_ext_content_raw', 'value': {'android.intent.extra.TEXT': 'Central Park'},
                 'locale': 'en-US', 'timestamp_ms': '1500'}


def test_client_builds_requests():
    assert car.build(['battery', '--say']) == ('GET', '/battery?say=1', None)
    assert car.build(['nav', 'the park']) == ('POST', '/nav', {'q': 'the park'})
    assert car.build(['cmd', 'climate-set-temp', '70f']) == ('POST', '/cmd/climate-set-temp', {'arg': '70f'})


def test_nav_intent():
    assert nav_intent.place_from('Navigate to the coffee shop.') == 'coffee shop'
    assert nav_intent.place_from('take me to central park') == 'central park'
    assert nav_intent.place_from('car, directions to the museum') == 'museum'
    assert nav_intent.place_from('map it') is None
    assert nav_intent.place_from("where's the car") is None
