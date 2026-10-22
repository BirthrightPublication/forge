"""Measured elapsed values; wall clock timestamps are only for display."""
import time


class JobClock:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started = self.changed = clock()
        self.stage = 'Starting'
        self.stages = {}

    def enter(self, stage):
        if stage and stage != self.stage:
            now = self.clock()
            self.stages[self.stage] = self.stages.get(self.stage, 0) + max(0, now - self.changed)
            self.changed, self.stage = now, stage

    def snapshot(self):
        now = self.clock()
        stages = dict(self.stages)
        stages[self.stage] = stages.get(self.stage, 0) + max(0, now - self.changed)
        return {'execution_seconds': max(0, now - self.started), 'stage_seconds': stages}
