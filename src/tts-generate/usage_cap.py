"""Monthly character cap for google_tts — keeps Google Cloud TTS spend just
past its free tier.

Standard voices are free for 4,000,000 chars per billing month, then $4 per
1M. Text-to-Speech has no per-day/per-month character quota that could be
set in Cloud Quotas (only requests/min), so the cap lives here: every
google_tts request is checked before the API call, and once the month's
total would pass GOOGLE_TTS_MONTHLY_CHARS the request is served by Piper
instead (see main.generate). Default 4,100,000 = free tier + 2.5%, i.e. at
most ~100k paid chars ≈ $0.40 ≈ 10,000 VND a month — same "only a little
over free" rule as novel-web's Translate cap (510k vs 500k free).

Counting is sharded per instance: each Cloud Run instance keeps its own
running total in usage/google_tts/<month>/<instance>.json and the month's
total is the sum of every shard in that folder. A single shared object was
tried first, but GCS allows ~1 write/s per object and the reader prefetches
up to 10 sentences at once (10 instances, concurrency 1), so most of a burst
lost the race and fell back to Piper mid-chapter. With shards nobody
contends; the price is that other instances' totals are read at most
SYNC_SECONDS late, so a burst can overshoot the cap by a few thousand chars
— well inside the 100k margin. Months are Pacific time, like Google's
billing month.

Own shard writes are throttled to one per FLUSH_SECONDS (GCS per-object
rate limit) and flushed on exit, since a Cloud Run instance gets CPU only
while serving a request or shutting down — no background thread.

Fails closed: if usage can't be read, the request is treated as over the
cap (Piper is free; an unmetered Google call isn't).
"""
import atexit
import json
import os
import threading
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

GOOGLE_TTS_MONTHLY_CHARS = int(os.environ.get("GOOGLE_TTS_MONTHLY_CHARS", "4100000"))
USAGE_BUCKET = os.environ.get("USAGE_BUCKET", "")
USAGE_PREFIX = "usage/google_tts"
SYNC_SECONDS = 30
FLUSH_SECONDS = 2


def _pacific_month():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m")


class UsageCounter:
    def __init__(self, bucket=None, instance_id=None, clock=time.monotonic):
        self._bucket = bucket
        self._instance = instance_id or uuid.uuid4().hex[:12]
        self._clock = clock
        self._lock = threading.Lock()
        self._month = None
        self._own = 0           # this instance's chars this month
        self._flushed = 0       # part of _own already written to its shard
        self._last_flush = float("-inf")
        self._others = 0        # sum of the other shards at last sync
        self._last_sync = float("-inf")

    def _get_bucket(self):
        if self._bucket is None:
            from google.cloud import storage
            self._bucket = storage.Client().bucket(USAGE_BUCKET)
        return self._bucket

    def _shard(self, month):
        return f"{USAGE_PREFIX}/{month}/{self._instance}.json"

    def _sync(self, month):
        own_name = self._shard(month)
        others = 0
        for blob in self._get_bucket().list_blobs(prefix=f"{USAGE_PREFIX}/{month}/"):
            if blob.name != own_name:
                others += json.loads(blob.download_as_bytes()).get("chars", 0)
        self._others, self._last_sync = others, self._clock()

    def _flush(self, force=False):
        if self._month is None or self._own == self._flushed:
            return
        if not force and self._clock() - self._last_flush < FLUSH_SECONDS:
            return
        self._get_bucket().blob(self._shard(self._month)).upload_from_string(
            json.dumps({"chars": self._own}), content_type="application/json")
        self._flushed, self._last_flush = self._own, self._clock()

    def charge(self, chars, month=None, limit=None):
        """Counts `chars` and returns True, or returns False (counting
        nothing) if that would pass the month's cap or usage is unreadable."""
        limit = GOOGLE_TTS_MONTHLY_CHARS if limit is None else limit
        month = month or _pacific_month()
        with self._lock:
            try:
                if month != self._month:
                    self._flush(force=True)
                    self._month, self._own, self._flushed = month, 0, 0
                    self._last_sync = self._last_flush = float("-inf")
                if self._clock() - self._last_sync >= SYNC_SECONDS:
                    self._sync(month)
            except Exception as e:
                print(f"tts-generate: google_tts usage read error, failing closed: {e}", flush=True)
                return False
            used = self._others + self._own
            if used + chars > limit:
                print(f"tts-generate: google_tts cap reached ({used:,}+{chars} > {limit:,} chars in {month})", flush=True)
                return False
            self._own += chars
            try:
                self._flush()
            except Exception as e:
                # Counted locally; the next flush (or exit) writes it.
                print(f"tts-generate: google_tts usage write deferred: {e}", flush=True)
            return True

    def close(self):
        with self._lock:
            try:
                self._flush(force=True)
            except Exception as e:
                print(f"tts-generate: google_tts usage final flush failed: {e}", flush=True)


_counter = UsageCounter()
atexit.register(_counter.close)


def charge(chars):
    return _counter.charge(chars)
