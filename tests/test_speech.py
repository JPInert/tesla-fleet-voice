import speech


def test_battery_usable_first_and_total_when_different():
    s = speech.battery_sentence({'usable_battery_level': 79, 'battery_level': 80,
                                 'battery_range': 282.4, 'charging_state': 'Disconnected'})
    assert s == "The car's at 79 percent usable, 80 total, about 282 miles."


def test_battery_never_says_unplugged():
    s = speech.battery_sentence({'usable_battery_level': 50, 'battery_level': 50, 'charging_state': 'Disconnected'})
    assert 'plug' not in s.lower()
    assert s == "The car's at 50 percent."


def test_battery_charging_states():
    assert speech.battery_sentence({'usable_battery_level': 60, 'charging_state': 'Charging'}).endswith('. Charging.')
    assert 'finished charging' in speech.battery_sentence({'usable_battery_level': 80, 'charging_state': 'Complete'})
    assert 'Plugged in, stopped' in speech.battery_sentence({'usable_battery_level': 80, 'charging_state': 'Stopped'})


def test_temperature_converts_to_fahrenheit():
    s = speech.temp_sentence({'inside_temp': 35.6, 'outside_temp': 30.0,
                              'is_climate_on': True, 'driver_temp_setting': 22.2})
    assert s == "The car's 96 degrees inside, 86 outside, climate on, set to 72."


def test_temperature_missing():
    assert speech.temp_sentence({}) == "The car didn't report a temperature."


def test_spoken_name_override(monkeypatch):
    monkeypatch.setenv('CAR_SPOKEN_NAME', 'the truck')
    assert speech.temp_sentence({}) == "The truck didn't report a temperature."


def test_ago():
    assert speech.ago(1000, now=1000 + 60) == 'just now'
    assert speech.ago(0, now=45 * 60) == '45 minutes ago'
    assert speech.ago(0, now=3 * 3600) == '3 hours ago'
    assert speech.ago(0, now=3 * 86400) == '3 days ago'
