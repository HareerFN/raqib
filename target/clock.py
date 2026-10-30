"""Virtual clock: the whole system runs on simulated seconds.

One hour of system time runs in about a second of real time, so a full
benchmark of many scenarios finishes in minutes and is fully reproducible.
"""


class VirtualClock:
    def __init__(self, start: int = 0):
        self.now = start

    def tick(self, seconds: int = 1) -> int:
        self.now += seconds
        return self.now
