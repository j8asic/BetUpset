"""
Benchmark: Soccer market fetching from Polymarket and Kalshi.

Instruments every API call and phase to identify latency bottlenecks.
"""

import sys
import time
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional
import statistics


# ---------------------------------------------------------------------------
# Timing infrastructure
# ---------------------------------------------------------------------------

@dataclass
class CallRecord:
    label: str
    elapsed_ms: float
    status: str  # "ok" or "error"


@dataclass
class PhaseRecord:
    name: str
    start: float = field(default_factory=time.perf_counter)
    calls: list[CallRecord] = field(default_factory=list)
    end: Optional[float] = None

    def finish(self):
        self.end = time.perf_counter()

    @property
    def elapsed_ms(self):
        if self.end is None:
            return (time.perf_counter() - self.start) * 1000
        return (self.end - self.start) * 1000


class BenchmarkSession:
    def __init__(self, name: str):
        self.name = name
        self.phases: list[PhaseRecord] = []
        self._current_phase: Optional[PhaseRecord] = None
        self._start = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        p = PhaseRecord(name)
        self.phases.append(p)
        self._current_phase = p
        try:
            yield p
        finally:
            p.finish()
            self._current_phase = None

    def record_call(self, label: str, elapsed_ms: float, status: str = "ok"):
        rec = CallRecord(label, elapsed_ms, status)
        if self._current_phase:
            self._current_phase.calls.append(rec)

    @property
    def total_ms(self):
        return (time.perf_counter() - self._start) * 1000

    def report(self):
        print(f"\n{'=' * 65}")
        print(f"  BENCHMARK: {self.name}")
        print(f"  Total elapsed: {self.total_ms:.0f} ms")
        print(f"{'=' * 65}")

        for p in self.phases:
            n_calls = len(p.calls)
            ok = sum(1 for c in p.calls if c.status == "ok")
            err = n_calls - ok
            print(f"\n  Phase: {p.name}")
            print(f"    Duration : {p.elapsed_ms:>7.0f} ms")
            print(f"    API calls: {n_calls}  (ok={ok}, err={err})")

            if p.calls:
                times = [c.elapsed_ms for c in p.calls]
                print(f"    Per-call : min={min(times):.0f}  "
                      f"med={statistics.median(times):.0f}  "
                      f"max={max(times):.0f}  "
                      f"mean={statistics.mean(times):.0f} ms")

                # Show slowest calls
                slowest = sorted(p.calls, key=lambda c: -c.elapsed_ms)[:5]
                if slowest:
                    print(f"    Slowest calls:")
                    for c in slowest:
                        status_tag = "" if c.status == "ok" else f" [{c.status}]"
                        print(f"      {c.elapsed_ms:>6.0f} ms  {c.label}{status_tag}")

        # Breakdown bar
        print(f"\n  Phase breakdown (% of total {self.total_ms:.0f} ms):")
        for p in self.phases:
            pct = p.elapsed_ms / self.total_ms * 100
            bar = "#" * int(pct / 2)
            print(f"    {p.name:<35} {p.elapsed_ms:>6.0f} ms  {pct:4.0f}%  {bar}")
        print()


# ---------------------------------------------------------------------------
# Patch helpers — monkey-patch _get on clients to record timing
# ---------------------------------------------------------------------------

def patch_client(client, session: BenchmarkSession):
    """
    Wrap the client's _get method to record timing per call.
    Also wraps _rate_limit to measure sleep overhead, and patches
    session.get to catch direct HTTP calls (e.g. CLOB pre-kickoff fetches).
    """
    original_get = client._get
    original_rate_limit = client._rate_limit
    original_session_get = client.session.get
    rate_sleep_total = [0.0]
    direct_call_count = [0]

    def timed_rate_limit():
        t0 = time.perf_counter()
        original_rate_limit()
        slept = (time.perf_counter() - t0) * 1000
        rate_sleep_total[0] += slept

    def timed_get(endpoint, params=None):
        t0 = time.perf_counter()
        result = original_get(endpoint, params=params)
        elapsed = (time.perf_counter() - t0) * 1000
        status = "ok" if result is not None else "error/empty"
        session.record_call(endpoint, elapsed, status)
        return result

    def timed_session_get(url, **kwargs):
        # Only track calls that go outside the _get wrapper
        t0 = time.perf_counter()
        result = original_session_get(url, **kwargs)
        elapsed = (time.perf_counter() - t0) * 1000
        direct_call_count[0] += 1
        label = url.split("//")[-1].split("?")[0]  # strip domain + query
        session.record_call(f"[direct] {label}", elapsed,
                            "ok" if result.status_code == 200 else f"http{result.status_code}")
        return result

    client._get = timed_get
    client._rate_limit = timed_rate_limit
    client.session.get = timed_session_get
    client._rate_sleep_total = rate_sleep_total
    client._direct_call_count = direct_call_count
    return rate_sleep_total


# ---------------------------------------------------------------------------
# Kalshi benchmark
# ---------------------------------------------------------------------------

def benchmark_kalshi():
    print("\n[Kalshi] Starting benchmark (parallel fetch_soccer_markets)...")
    from kalshi_client import KalshiClient
    client = KalshiClient()

    session = BenchmarkSession("Kalshi — fetch_soccer_markets() [parallel]")
    patch_client(client, session)

    results = []
    with session.phase("fetch_soccer_markets() — parallel series"):
        results = client.fetch_soccer_markets()

    print(f"[Kalshi] NormalizedMatches: {len(results)}")
    session.report()
    return session


# ---------------------------------------------------------------------------
# Polymarket benchmark
# ---------------------------------------------------------------------------

def benchmark_polymarket():
    print("\n[Polymarket] Starting benchmark (with pre-kickoff cache)...")
    from polymarket_client import PolymarketClient
    client = PolymarketClient()

    session = BenchmarkSession("Polymarket — fetch_soccer_markets() [cold cache]")
    patch_client(client, session)

    results = []
    with session.phase("fetch_soccer_markets() — cold pre-kickoff cache"):
        results = client.fetch_soccer_markets()

    print(f"[Polymarket] NormalizedMatches: {len(results)}")
    session.report()

    # Second run: shows warm-cache benefit
    session2 = BenchmarkSession("Polymarket — fetch_soccer_markets() [warm cache]")
    patch_client(client, session2)
    print("\n[Polymarket] Second run (warm pre-kickoff cache)...")
    with session2.phase("fetch_soccer_markets() — warm pre-kickoff cache"):
        client.fetch_soccer_markets()
    session2.report()

    return session


# ---------------------------------------------------------------------------
# Side-by-side summary
# ---------------------------------------------------------------------------

def summary(kal: BenchmarkSession, poly: BenchmarkSession):
    print(f"\n{'=' * 65}")
    print("  SUMMARY: Fetch time comparison")
    print(f"{'=' * 65}")
    print(f"  {'Platform':<20} {'Total (ms)':>12}  {'API calls':>10}")
    print(f"  {'-' * 50}")

    for s in (kal, poly):
        total_calls = sum(len(p.calls) for p in s.phases)
        print(f"  {s.name.split('—')[0].strip():<20} {s.total_ms:>12.0f}  {total_calls:>10}")

    print(f"\n  Phase details:")
    for s in (kal, poly):
        print(f"\n  [{s.name.split('—')[0].strip()}]")
        for p in s.phases:
            n = len(p.calls)
            print(f"    {p.name:<40} {p.elapsed_ms:>6.0f} ms  ({n} calls)")

    print(f"\n  KEY FINDINGS:")
    for s in (kal, poly):
        platform = s.name.split("—")[0].strip()
        for p in s.phases:
            if p.calls:
                times = [c.elapsed_ms for c in p.calls]
                net_ms = sum(times)
                sleep_portion = 0.0
                # Estimate: elapsed - network time = sleep overhead
                overhead = p.elapsed_ms - net_ms
                if overhead > 100:
                    print(f"  [{platform}] '{p.name}': "
                          f"{p.elapsed_ms:.0f} ms total, "
                          f"{net_ms:.0f} ms network, "
                          f"~{overhead:.0f} ms overhead (sleep/parse)")
    print()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("SafeBet fetch benchmark")
    print(f"Running from: {__import__('os').getcwd()}")

    target = sys.argv[1] if len(sys.argv) > 1 else "both"

    kal_session = None
    poly_session = None

    if target in ("kalshi", "both"):
        kal_session = benchmark_kalshi()

    if target in ("polymarket", "both"):
        poly_session = benchmark_polymarket()

    if kal_session and poly_session:
        summary(kal_session, poly_session)
