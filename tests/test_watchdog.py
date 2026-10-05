import watchdog


def test_secure_car_has_nothing_open():
    v = {k: 0 for k in watchdog.OPENINGS}
    v['locked'] = True
    assert watchdog.open_things(v) == []


def test_open_and_unlocked_phrase():
    v = {k: 0 for k in watchdog.OPENINGS}
    v.update(fd_window=1, rt=1, locked=False)
    opened = watchdog.open_things(v)
    assert opened == ['driver window', 'trunk', 'unlocked']
    assert watchdog.phrase(opened) == 'has the driver window and trunk open and is unlocked'


def test_unknown_lock_state_is_not_unlocked():
    assert watchdog.open_things({}) == []


def test_say_list():
    assert watchdog.say_list(['a']) == 'a'
    assert watchdog.say_list(['a', 'b', 'c']) == 'a, b and c'



def test_alert_sentences_read_right():
    import speech
    c = {'usable_battery_level': 12, 'battery_range': 40.2}
    assert f'Heads up, {speech.describe(c)}, and not charging.' == \
        "Heads up, the car's at 12 percent, about 40 miles, and not charging."
    assert speech.cap(speech.describe(c)) == "The car's at 12 percent, about 40 miles"
