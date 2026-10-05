"""Free-form "navigate to <place>" for a voice daemon.

A fixed phrase list cannot carry a captured place name, so this is a few lines of code in the
voice daemon instead of a trick. It runs AFTER the phrase list, so "navigate home" is still
the saved-place trick. The call is fire-and-forget because waking the car can take longer
than a voice command's timeout.

    navigate to / map / route us to / take me to / directions to <place>

Refuses "map it", "take me to it", etc. via the lookahead.
"""
import re
import subprocess

NAV_RE = re.compile(
    r"^\W*(?:please\s+)?(?:(?:the\s+)?(?:car|tesla)\W+)?"
    r"(?:(?:navigate|map)(?:\s+(?:the\s+)?(?:car|tesla))?(?:\W+to\b)?"
    r"|(?:route|take|drive)(?:\s+(?:me|us))?\s+to|(?:get\s+)?directions\s+to)\W+"
    r"(?!(?:of|out|it|this|that|me|us|up)\b)(?:the\s+)?(?P<place>\w.*?)[\s.,!?\"]*$", re.I)


def place_from(text):
    """'Navigate to the coffee shop.' -> 'coffee shop'; None when it is not a nav request."""
    m = NAV_RE.match(text)
    return m.group('place') if m else None


def try_nav(text, car_py):
    place = place_from(text)
    if not place:
        return False
    subprocess.Popen(['python3', car_py, 'nav', place, '--say'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True
