class FakeScheduler:
    def __init__(self):
        self.runs = 0
        self.pins: dict[str, str] = {}

    def get_pinned_strategy(self, domain: str):
        return self.pins.get(domain)

    async def run(self, jobs, worker, on_result=None, *, failed_result=None):
        self.runs += 1
        out = {}
        for job in jobs:
            try:
                out[job.index] = await worker(job)
            except Exception as error:
                if failed_result is None:
                    raise
                out[job.index] = failed_result(job, error)
        return out


__all__ = ["FakeScheduler"]
