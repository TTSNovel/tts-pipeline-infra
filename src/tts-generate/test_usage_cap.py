"""Standalone sanity check for usage_cap.UsageCounter — fake in-memory GCS
bucket and clock, no network, run directly:

    python3 test_usage_cap.py

Covers: counting up to the cap and refusing past it without counting, other
instances' shards (incl. a seed shard) counting toward the cap after a sync,
month rollover, throttled shard writes plus the final flush, a shard
rewritten between list and download, and failing closed when GCS is unreadable.
"""
import json

from google.api_core.exceptions import NotFound, ServiceUnavailable

import usage_cap
from usage_cap import UsageCounter


class FakeBlob:
    def __init__(self, bucket, name, generation=None):
        self.bucket, self.name, self.generation = bucket, name, generation

    def download_as_bytes(self):
        if self.generation is not None and self.generation != self.bucket.gens[self.name]:
            raise NotFound("stale generation")
        return self.bucket.objects[self.name]

    def upload_from_string(self, data, content_type=None):
        self.bucket.writes += 1
        self.bucket.objects[self.name] = data.encode()
        self.bucket.gens[self.name] = self.bucket.gens.get(self.name, 0) + 1


class FakeBucket:
    def __init__(self, broken=False):
        self.objects, self.gens, self.writes, self.broken = {}, {}, 0, broken
        self.after_list = None

    def blob(self, name):
        return FakeBlob(self, name)

    def list_blobs(self, prefix):
        if self.broken:
            raise ServiceUnavailable("gcs down")
        # Like GCS: listed blobs carry the generation current at list time.
        listed = [FakeBlob(self, n, self.gens[n]) for n in sorted(self.objects) if n.startswith(prefix)]
        if self.after_list:
            self.after_list()
        return listed

    def put(self, name, chars):
        self.objects[name] = json.dumps({"chars": chars}).encode()
        self.gens[name] = self.gens.get(name, 0) + 1

    def chars(self, name):
        return json.loads(self.objects[name])["chars"]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    return cond


P = usage_cap.USAGE_PREFIX
ok = True

b, clock = FakeBucket(), Clock()
c = UsageCounter(b, "a", clock)
ok &= check("first charge writes own shard", c.charge(400, "2026-11", 1000) and b.chars(f"{P}/2026-11/a.json") == 400)
clock.t += 5
ok &= check("charge up to exactly the cap", c.charge(600, "2026-11", 1000) and b.chars(f"{P}/2026-11/a.json") == 1000)
ok &= check("refuse past cap, nothing counted", not c.charge(1, "2026-11", 1000) and b.chars(f"{P}/2026-11/a.json") == 1000)
ok &= check("new month starts from zero", c.charge(300, "2026-12", 1000) and b.chars(f"{P}/2026-12/a.json") == 300)

b, clock = FakeBucket(), Clock()
b.put(f"{P}/2026-11/seed.json", 900)
b.put(f"{P}/2026-10/old.json", 5000)
c = UsageCounter(b, "a", clock)
ok &= check("seed shard counts, other months don't", c.charge(100, "2026-11", 1000) and not c.charge(1, "2026-11", 1000))

b, clock = FakeBucket(), Clock()
a, other = UsageCounter(b, "a", clock), UsageCounter(b, "b", clock)
a.charge(100, "2026-11", 1000)
other.charge(800, "2026-11", 1000)
ok &= check("other instance unseen until next sync", a.charge(50, "2026-11", 1000))
clock.t += usage_cap.SYNC_SECONDS
ok &= check("after sync other instance counts", not a.charge(100, "2026-11", 1000) and a.charge(50, "2026-11", 1000))

b, clock = FakeBucket(), Clock()
c = UsageCounter(b, "a", clock)
for _ in range(10):
    c.charge(10, "2026-11", 10_000)
ok &= check("burst within FLUSH_SECONDS writes once", b.writes == 1 and b.chars(f"{P}/2026-11/a.json") == 10)
c.close()
ok &= check("close flushes the rest", b.chars(f"{P}/2026-11/a.json") == 100)

b, clock = FakeBucket(), Clock()
b.put(f"{P}/2026-11/b.json", 100)
b.after_list = lambda: b.put(f"{P}/2026-11/b.json", 200)  # b flushes between list and download
ok &= check("shard rewritten mid-sync still reads", UsageCounter(b, "a", clock).charge(800, "2026-11", 1000)
            and not UsageCounter(b, "c", clock).charge(801, "2026-11", 1000))

ok &= check("unreadable usage fails closed", not UsageCounter(FakeBucket(broken=True), "a", Clock()).charge(10, "2026-11", 1000))

raise SystemExit(0 if ok else 1)
