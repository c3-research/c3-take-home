"""Retry pacing shared by the worker and the router.

Every retry loop in the service sleeps between attempts with a bounded
exponential backoff, so a slow or unreachable peer is not flooded with
requests. Jitter comes from the node's seeded `rng`, the only permitted source
of randomness.
"""

from . import config


class Backoff:
    """Exponential backoff: base, 2*base, 4*base, ... capped at `cap`."""

    def __init__(self, rng=None, base=config.GPU_BACKOFF_BASE_S, cap=config.GPU_BACKOFF_MAX_S,
                 jitter=0.25):
        self._rng = rng
        self.base = float(base)
        self.cap = float(cap)
        self.jitter = float(jitter)
        self.attempts = 0

    def next(self):
        """Delay before the next attempt (and count the attempt)."""
        d = min(self.cap, self.base * (2 ** self.attempts))
        self.attempts += 1
        if self._rng is not None and self.jitter > 0:
            d *= 1.0 + self._rng.uniform(0.0, self.jitter)
        return d

    def reset(self):
        self.attempts = 0


def idle_delay(rng):
    """Pause before a worker polls the router again after finding no work."""
    return config.ACQUIRE_IDLE_S + rng.uniform(0.0, config.ACQUIRE_JITTER_S)
