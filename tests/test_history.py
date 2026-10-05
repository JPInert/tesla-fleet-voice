from datetime import datetime

import history

DAY = datetime(2026, 1, 15, 9, 0).timestamp()   # local 09:00, so all rows land on one local day


def test_scrub_removes_identifiers():
    body = {'vin': 'X', 'id': 1, 'id_s': '1', 'vehicle_id': 2,
            'vehicle_state': {'odometer': 10, 'vin': 'X'}, 'list': [{'id': 3, 'ok': 1}]}
    assert history._scrub(body) == {'vehicle_state': {'odometer': 10}, 'list': [{'ok': 1}]}


def test_days_counts_miles_charge_and_parked_drain(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(history, 'DB', tmp_path / 'h.db')
    monkeypatch.setattr(history.time, 'time', lambda: DAY + 3 * 3600)
    rows = [  # (offset s, usable %, odometer)
        (0, 80, 1000.0),
        (3600, 78, 1000.0),       # parked: 2 % drain
        (7200, 70, 1012.5),       # drove 12.5 mi: not drain
        (9000, 75, 1012.5),       # charged 5 %
    ]
    for off, pct, odo in rows:
        history.record_reading(DAY + off, 'online', {'usable_battery_level': pct}, {'odometer': odo}, {})
    history.show_days(1)
    out = capsys.readouterr().out.splitlines()
    day = [line for line in out if line[:4].isdigit()]
    assert len(day) == 1
    cols = day[0].split()
    assert cols[1:] == ['4', '70', '80', '12.5', '5', '2']


def test_state_only_stores_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(history, 'DB', tmp_path / 'h.db')
    for i, s in enumerate(['online', 'online', 'asleep', 'asleep', 'online']):
        history.record_state(DAY + i, s)
    with history.connect() as con:
        assert [r[0] for r in con.execute('SELECT state FROM states ORDER BY ts')] == ['online', 'asleep', 'online']
