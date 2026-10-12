"""The place resolver, add-a-stop, confirm-before-wake and volume stepping, against fakes only
(conftest's `offline` fixture: no network, no car)."""
import json
import time

import pytest

import car
import car_service as cs
import config
import nav_intent

HOME = ('40.7484', '-73.9857')         # public landmark, as in fleet.example.env
NEAR_CAR = (40.7794, -73.9632)         # where the fake car is parked in these tests


def set_home(monkeypatch):
    monkeypatch.setenv('HOME_LAT', HOME[0])
    monkeypatch.setenv('HOME_LON', HOME[1])


def online_at(fleet, lat, lon):
    fleet.routes[('GET', '/vehicle_data?endpoints=drive_state')] = (
        200, {'response': {'drive_state': {'latitude': lat, 'longitude': lon}}})


def hit(name, lat, lon, **address):
    return {'name': name, 'lat': str(lat), 'lon': str(lon), 'address': address}


# ── cleaning what was said ───────────────────────────────────────────────

def test_strip_car_drops_a_trailing_car_name(monkeypatch):
    assert cs.strip_car('pizza place on the car') == 'pizza place'
    assert cs.strip_car('Bakery, in the Tesla.') == 'bakery'
    assert cs.strip_car('pizza on main street') == 'pizza on main street'   # not a car word
    monkeypatch.setenv('CAR_ALIASES', 'rocket, rokit')
    assert cs.strip_car('noodle bar on rokit') == 'noodle bar'
    monkeypatch.setenv('CAR_SPOKEN_NAME', 'the blue car')
    assert cs.strip_car('deli to blue') == 'deli'


def test_recall_query_spots_a_reference_back():
    assert cs.recall_query('that ice cream place i mapped before') == ('ice cream place', True)
    assert cs.recall_query('the bakery again') == ('bakery', True)
    assert cs.recall_query('that pizza place on main street') == ('pizza place on main street', False)


# ── destination memory ───────────────────────────────────────────────────

def test_history_match_prefix_and_newest_wins():
    cs.record_nav('ice cream', 'Old Ice Cream Parlor', 'Old Ice Cream Parlor, 1 A St', geocode=False)
    cs.record_nav('italian', 'Corner Italian Kitchen', 'Corner Italian Kitchen, 2 B St', geocode=False)
    cs.record_nav('ice cream', 'New Ice Cream Parlor', 'New Ice Cream Parlor, 3 C St', geocode=False)
    assert cs.history_match('ital place')['label'] == 'Corner Italian Kitchen'
    assert cs.history_match('ice cream')['label'] == 'New Ice Cream Parlor'
    assert cs.history_match('sushi') is None


def test_history_keeps_one_entry_per_address():
    for _ in range(3):
        cs.record_nav('bakery', 'Bakery', 'Bakery, 1 A St', geocode=False)
    cs.record_nav('bakery', 'Bakery', 'Bakery, 9 Z St', geocode=False)    # a second branch stays separate
    assert [e['address'] for e in json.loads(cs.NAV_HISTORY.read_text())] == ['Bakery, 1 A St', 'Bakery, 9 Z St']


# ── resolver order ───────────────────────────────────────────────────────

def test_resolver_order_saved_then_memory_then_search(offline, monkeypatch):
    set_home(monkeypatch)
    config.PLACES.write_text(json.dumps({'museum': 'Museum, 1000 5th Ave'}))
    cs.record_nav('museum', 'Some Other Museum', 'Some Other Museum, 5 X St', geocode=False)
    cs.record_nav('bakery', 'Corner Bakery', 'Corner Bakery, 7 Y St', geocode=False)
    assert cs.resolve_place({'q': 'the museum'}) == ('museum', 'Museum, 1000 5th Ave')          # saved first
    assert cs.resolve_place({'q': 'bakery on the car'}) == ('Corner Bakery', 'Corner Bakery, 7 Y St')  # memory
    assert offline['net']['nominatim'] == []                     # neither touched the map search
    assert cs.resolve_place({'address': '1 Main St'}) == ('1 Main St', '1 Main St')
    cs.resolve_place({'q': 'hardware store'})                    # nothing saved or remembered: map search
    assert len(offline['net']['nominatim']) == 1


def test_reference_back_that_misses_says_so():
    with pytest.raises(ValueError, match="I don't have"):
        cs.resolve_place({'q': 'that noodle place we went to before'})


def test_far_remembered_chain_loses_to_branches_near_the_car(offline, monkeypatch):
    online_at(offline['fleet'], *NEAR_CAR)
    cs.record_nav('burger', 'Burger Chain', 'Burger Chain, 1 Far Rd', geocode=False)
    cs.GEO_CACHE.write_text(json.dumps({'Burger Chain, 1 Far Rd': [41.20, -73.70]}))    # ~30 mi away
    near = [hit('Burger Chain', 40.780, -73.964, house_number='5', road='Near St', city='New York'),
            hit('Burger Chain', 40.790, -73.950, house_number='9', road='Other St', city='New York')]
    monkeypatch.setattr(cs, '_nominatim_get', lambda url: near)
    label, addr = cs.resolve_place({'q': 'burger chain'})
    assert addr.startswith('Burger Chain, 5 Near St')


# ── search near the CAR ──────────────────────────────────────────────────

def test_nearest_is_measured_from_the_car_not_home(offline, monkeypatch):
    set_home(monkeypatch)
    online_at(offline['fleet'], *NEAR_CAR)
    hits = [hit('Cafe By Home', 40.7485, -73.9858, house_number='1', road='Home St', city='New York', state='New York'),
            hit('Cafe By Car', 40.7795, -73.9633, house_number='2', road='Car St', city='New York', state='New York')]
    label, addr = cs.search_near_home('cafe', fetch=lambda url: hits)
    assert label == 'Cafe By Car'
    assert json.loads(cs.LAST_POS.read_text())['lat'] == NEAR_CAR[0]     # remembered for when it sleeps


def test_asleep_car_uses_its_last_fix(offline, monkeypatch):
    set_home(monkeypatch)
    cs.atomic(cs.LAST_POS, {'lat': NEAR_CAR[0], 'lon': NEAR_CAR[1], '_at': time.time()})
    assert cs.locate() == 'last fix'
    assert cs._ctx.origin == NEAR_CAR
    assert not offline['fleet'].posts()                           # a location read never wakes


def test_address_uses_the_map_results_state(monkeypatch):
    set_home(monkeypatch)
    hits = [hit('Deli', 40.75, -73.98, house_number='3', road='C St', city='Albany', state='New York', postcode='12207')]
    assert cs.search_near_home('deli', fetch=lambda url: hits)[1] == 'Deli, 3 C St, Albany, New York, 12207'


def test_osm_miss_without_a_key_goes_to_the_car_raw(offline, monkeypatch):
    set_home(monkeypatch)
    assert cs.search_near_home('tiny noodle bar near me') == ('tiny noodle bar', 'tiny noodle bar')
    assert offline['net']['gemini'] == []                         # no key, no Google call


def test_osm_miss_with_a_key_asks_google(offline, monkeypatch):
    set_home(monkeypatch)
    monkeypatch.setenv('GEMINI_API_KEY', 'test-placeholder')
    monkeypatch.setattr(cs, '_gemini_post', lambda body, timeout: '{"name": "Tiny Noodle Bar", "address": "8 D St, New York, NY"}')
    assert cs.search_near_home('tiny nodle bar') == ('Tiny Noodle Bar', 'Tiny Noodle Bar, 8 D St, New York, NY')


def test_thin_osm_chain_is_checked_against_google(offline, monkeypatch):
    online_at(offline['fleet'], *NEAR_CAR)
    monkeypatch.setenv('GEMINI_API_KEY', 'test-placeholder')
    osm = [hit('Donut Chain', 40.8200, -73.9632, house_number='1', road='Far Ave', city='New York')]    # ~2.8 mi
    google = [{'name': 'Donut Chain', 'address': 'Donut Chain, 40.7850,-73.9632'},                      # ~0.4 mi
              {'name': 'Donut Chain', 'address': 'Donut Chain, 40.9000,-73.9632'}]
    monkeypatch.setattr(cs, '_gemini_post', lambda body, timeout: json.dumps(google))
    label, addr = cs.search_near_home('donut chain', fetch=lambda url: osm)
    assert addr == 'Donut Chain, Donut Chain, 40.7850,-73.9632'


def test_geocode_drops_house_number_and_state_as_a_last_try(monkeypatch):
    set_home(monkeypatch)
    asked = []

    def fetch(url):
        q = __import__('urllib.parse').parse.parse_qs(url.split('?', 1)[1])['q'][0]
        asked.append(q)
        return [hit('', 40.75, -73.98, road='Elm Street', city='Springfield')] if q == 'Elm Street, Springfield' else []
    assert cs.geocode_address('Shop, 12 Elm Street, Springfield, NY 10001', fetch=fetch) == [40.75, -73.98]
    assert asked[-1] == 'Elm Street, Springfield'
    assert cs.geocode_address('Pin, 40.123456,-73.654321') == [40.123456, -73.654321]   # coordinates: no lookup


# ── navigation: dry, confirm-before-wake ─────────────────────────────────

def test_nav_no_wake_checks_state_before_sending(offline):
    # navigation_request to an offline car returns 200 and Tesla wakes it, so the check must come first
    offline['fleet'].routes[('GET', '')] = (200, {'response': {'state': 'offline'}})
    res = cs.nav({'q': 'museum', 'wake': False})
    assert res['asleep'] is True and not offline['fleet'].posts()


def test_nav_dry_sends_nothing(offline, monkeypatch):
    config.PLACES.write_text(json.dumps({'museum': 'Museum, 1000 5th Ave'}))
    res = cs.nav({'q': 'museum', 'dry': True})
    assert res['dry'] and res['to'] == 'Museum, 1000 5th Ave'
    assert not offline['fleet'].posts()


def test_nav_sends_and_remembers(offline):
    config.PLACES.write_text(json.dumps({'museum': 'Museum, 1000 5th Ave'}))
    offline['fleet'].routes[('POST', '/command/navigation_request')] = (200, {'response': {'result': True}})
    res = cs.nav({'q': 'museum'})
    assert res['ok'] and not res['woke']
    method, path, body = offline['fleet'].posts()[0]
    assert body['value'] == {'android.intent.extra.TEXT': 'Museum, 1000 5th Ave'}
    assert cs.history_match('museum')['address'] == 'Museum, 1000 5th Ave'


# ── add a stop ───────────────────────────────────────────────────────────

def test_stop_from_saved_coordinates_dry(offline):
    config.PLACES.write_text(json.dumps({'museum@': '40.7794,-73.9632'}))
    res = cs.add_stop({'q': 'the museum on the car', 'dry': True})
    assert (res['lat'], res['lon'], res['how']) == (40.7794, -73.9632, 'saved')
    assert not offline['fleet'].posts()


def test_send_stop_is_order_2(offline):
    config.PLACES.write_text(json.dumps({'museum@': '40.7794,-73.9632'}))
    offline['fleet'].routes[('POST', '/command/navigation_gps_request')] = (200, {'response': {'result': True}})
    res = cs.send_stop({'q': 'museum'})
    assert res['ok']
    assert offline['fleet'].posts() == [('POST', '/command/navigation_gps_request',
                                         {'lat': 40.7794, 'lon': -73.9632, 'order': 2})]


def test_stop_asleep_without_wake_is_answered_at_once(offline):
    offline['fleet'].routes[('GET', '')] = (200, {'response': {'state': 'asleep'}})
    assert cs.add_stop({'q': 'museum', 'wake': False})['asleep'] is True
    config.PLACES.write_text(json.dumps({'museum@': '40.7794,-73.9632'}))
    assert cs.send_stop({'q': 'museum', 'wake': False})['asleep'] is True     # 408 and no wake_up sent
    assert ('POST', '/wake_up', None) not in offline['fleet'].calls


def test_home_stop_uses_home_coordinates(monkeypatch):
    set_home(monkeypatch)
    assert cs.place_coords({'q': 'home'})[:4] == ('home', '', 40.7484, -73.9857)


# ── volume ───────────────────────────────────────────────────────────────

def test_volume_arg():
    assert cs.volume_arg('50%') == '5.0'
    assert cs.volume_arg('50') == '5.0'
    assert cs.volume_arg('7') == '7.0'          # a bare number up to 10 is the car's own scale
    assert cs.volume_arg('+2', cur=3.0) == '5.0'
    assert cs.volume_arg('-9', cur=3.0) == '0.0'
    with pytest.raises(ValueError):
        cs.volume_arg('loud')


def test_volume_set_steps_instead_of_setting(monkeypatch):
    monkeypatch.setattr(cs, 'media_volume', lambda: 1.67)
    sent = []
    monkeypatch.setattr(cs, 'signed', lambda name, arg=None, wake=True: sent.append((name, wake)) or {'ok': True})
    res = cs.volume_set('50%')
    assert res['steps'] == 10 and res['pending']
    for _ in range(100):
        if len(sent) == 10:
            break
        time.sleep(0.01)
    assert sent == [('volume-up', False)] * 10         # never the absolute media-set-volume


# ── client and voice examples ────────────────────────────────────────────

def test_client_flags():
    assert car.build(['stop', 'bakery', '--dry']) == ('POST', '/stop', {'q': 'bakery', 'dry': True})
    assert car.build(['cmd', 'media-next', '--no-wake']) == ('POST', '/cmd/media-next', {'wake': False})
    with pytest.raises(SystemExit):
        car.build(['cmd', 'honk', '--dry'])
    assert car.exit_code({'ok': False, 'asleep': True}) == 3
    assert car.exit_code({'ok': False}) == 1


def test_stop_intent():
    assert nav_intent.stop_from('Add a stop at the coffee shop.') == 'coffee shop'
    assert nav_intent.stop_from('stop at the pharmacy on the way') == 'pharmacy'
    assert nav_intent.stop_from('add the museum as a stop') == 'museum'
    assert nav_intent.stop_from('swing by later') is None
    assert nav_intent.stop_from('add milk') is None
    assert nav_intent.place_from('map the pizza place on the car') == 'pizza place'
