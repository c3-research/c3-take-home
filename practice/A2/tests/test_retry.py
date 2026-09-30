import random

from jobrouter import config
from jobrouter.retry import Backoff, idle_delay


def test_backoff_doubles_and_caps():
    b = Backoff(None, base=0.2, cap=1.0)
    assert [round(b.next(), 6) for _ in range(5)] == [0.2, 0.4, 0.8, 1.0, 1.0]
    b.reset()
    assert b.next() == 0.2


def test_backoff_jitter_bounded():
    b = Backoff(random.Random(1), base=0.2, cap=2.0, jitter=0.25)
    d = b.next()
    assert 0.2 <= d <= 0.25


def test_idle_delay_range():
    r = random.Random(3)
    for _ in range(20):
        d = idle_delay(r)
        assert config.ACQUIRE_IDLE_S <= d <= config.ACQUIRE_IDLE_S + config.ACQUIRE_JITTER_S
