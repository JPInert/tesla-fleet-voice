"""Free-form "navigate to <place>" for a voice daemon.

A fixed phrase list cannot carry a captured place name, so this is a few lines of code in the
voice daemon instead of a trick. It runs AFTER the phrase list, so "navigate home" is still
the saved-place trick. The call is fire-and-forget because waking the car can take longer
than a voice command's timeout.

    navigate to / map / route us to / take me to / directions to <place>

Refuses "map it", "take me to it", etc. via the lookahead.

Add a stop (car.py stop, before the current destination):

    add a stop at / swing by / stop at <place> on the way / add <place> as a stop
"""
import re
import subprocess

NAV_RE = re.compile(
    r"^\W*(?:please\s+)?(?:(?:the\s+)?(?:car|tesla)\W+)?"
    r"(?:(?:navigate|map)(?:\s+(?:the\s+)?(?:car|tesla))?(?:\W+to\b)?"
    r"|(?:route|take|drive)(?:\s+(?:me|us))?\s+to|(?:get\s+)?directions\s+to)\W+"
    r"(?!(?:of|out|it|this|that|me|us|up)\b)(?:the\s+)?(?P<place>\w.*?)[\s.,!?\"]*$", re.I)


CAR = r"(?:car|tesla)"     # add your car's name and its speech-to-text misspellings here
STOP_RE = re.compile(
    rf"^\W*(?:please\s+)?(?:(?:the\s+)?{CAR}\W+)?"
    r"(?:(?:add|make|put\s+in)\s+(?:a\s+)?stop(?:\s+(?:at|for|to))?|swing\s+by|stop\s+(?=.+\bon\s+the\s+way\b)(?:at|by))\W+"
    # "swing by later" is not a stop at a place called "later"
    r"(?!(?:later|soon|tomorrow|tonight|today|after|before|in\s+a\s+(?:bit|minute|while|second)|sometime|some\s+time|it|there|here)\b)"
    r"(?:the\s+)?(?P<place>\w.*?)(?:\s+on\s+the\s+way)?(?:\s+(?:as\s+a\s+stop|first))?[\s.,!?\"]*$", re.I)
# "add <place> as a stop" / "add <place> to the route": the tail is required, so "add milk" stays out
STOP_AS_RE = re.compile(
    rf"^\W*(?:please\s+)?(?:(?:the\s+)?{CAR}\W+)?add\s+(?:the\s+)?(?P<place>\w.*?)"
    r"\s+(?:as\s+(?:a\s+)?stop|to\s+(?:the\s+|my\s+|our\s+)?(?:route|trip|drive|navigation|nav))[\s.,!?\"]*$", re.I)
# a TRAILING car name says which car, it is not part of the place ("map the pizza place on the car")
CAR_TAIL = re.compile(rf"[\s,]+(?:on|in|to|for|with|into)\s+(?:the\s+)?{CAR}\W*$", re.I)


def place_from(text):
    """'Navigate to the coffee shop.' -> 'coffee shop'; None when it is not a nav request."""
    m = NAV_RE.match(text)
    return CAR_TAIL.sub('', m.group('place')) if m else None


def stop_from(text):
    """'Add a stop at the coffee shop' -> 'coffee shop'; None when it is not a stop request."""
    m = STOP_RE.match(text) or STOP_AS_RE.match(text)
    return CAR_TAIL.sub('', m.group('place')) if m else None


def try_nav(text, car_py):
    place = place_from(text)
    if not place:
        return False
    subprocess.Popen(['python3', car_py, 'nav', place, '--say'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def try_stop(text, car_py):
    place = stop_from(text)
    if not place:
        return False
    subprocess.Popen(['python3', car_py, 'stop', place, '--say'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
