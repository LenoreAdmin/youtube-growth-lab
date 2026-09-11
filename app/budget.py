import time


class SyncBudgetExceeded(Exception):
    """Committed checkpoints will be resumed by the next cron invocation."""


class Budget:
    def __init__(self, seconds=210):
        self.deadline = time.monotonic()+seconds

    def check(self):
        # Enough room for one bounded upstream request and DB cleanup.
        if self.deadline-time.monotonic() < 25:
            raise SyncBudgetExceeded()
