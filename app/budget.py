import time


class SyncBudgetExceeded(Exception):
    """Committed checkpoints will be resumed by the next cron invocation."""


class Budget:
    def __init__(self, seconds=210, reserve=0):
        self.deadline = time.monotonic()+seconds
        # Seconds withheld from the current phase so a saturated core import cannot starve
        # the optional learning/discovery phases; release() hands them back.
        self.reserve = reserve

    def check(self):
        # Enough room for one bounded upstream request and DB cleanup.
        if self.deadline-self.reserve-time.monotonic() < 25:
            raise SyncBudgetExceeded()

    def release(self):
        self.reserve = 0

    def remaining(self):
        return max(0.0, self.deadline-self.reserve-time.monotonic())
