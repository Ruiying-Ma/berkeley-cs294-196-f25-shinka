# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict, deque

# ARC state: T1/T2 (live), B1/B2 (ghosts), with p = target size of T1.
_T1 = OrderedDict()  # probation (recency)
_T2 = OrderedDict()  # protected (frequency)
_B1 = OrderedDict()  # ghost of T1 evictions
_B2 = OrderedDict()  # ghost of T2 evictions

# TinyLFU-like small saturating counters for keys in cache
_freq = {}           # key -> small int
_FREQ_MAX = 7
_last_age = 0        # last access_count when we aged the frequency table

# ARC adaptive target for T1 with momentum smoothing
_p = None            # target size for T1 (0..capacity)
_p_mom = 0.0

# Scan detector: sliding window of last W events
_scan_W = None
_scan_q = deque()
_scan_hits = 0
_scan_new = 0

# Touch gating during scans: require two touches before promotion
_touch = {}          # key -> recent touch count in probation
_REQUIRE_TWO_TOUCHES = True  # enabled during scan-mode


def _cap(cache_snapshot):
    cap = int(getattr(cache_snapshot, "capacity", 1) or 1)
    return max(cap, 1)


def _init(cache_snapshot):
    """Initialize parameters when capacity first known or changes."""
    global _p, _scan_W
    cap = _cap(cache_snapshot)
    if _p is None or _scan_W != max(2 * cap, 8):
        _p = max(0, min(cap, cap // 2))
        _scan_W = max(2 * cap, 8)


def _sync_segments(cache_snapshot):
    """Align T1/T2 membership to actual cache content."""
    cached = set(cache_snapshot.cache.keys())

    # Remove stale from live segments
    for seg in (_T1, _T2):
        for k in list(seg.keys()):
            if k not in cached:
                seg.pop(k, None)
                _touch.pop(k, None)

    # Add any cached keys missing from both segments to T1 MRU
    for k in cached:
        if k not in _T1 and k not in _T2:
            _T1[k] = None
            _touch[k] = 0


def _trim_ghosts(cap):
    """Bound total ghosts to 2×capacity, evicting oldest from the larger list first."""
    while len(_B1) + len(_B2) > 2 * cap:
        if len(_B1) >= len(_B2):
            # pop LRU from B1
            try:
                _B1.popitem(last=False)
            except KeyError:
                break
        else:
            try:
                _B2.popitem(last=False)
            except KeyError:
                break


def _age_frequency(now, cap):
    """Periodically age frequency counters to avoid stale bias."""
    global _last_age
    if now - _last_age >= cap:
        for k in list(_freq.keys()):
            _freq[k] >>= 1
            if _freq[k] == 0:
                _freq.pop(k, None)
        _last_age = now


def _bump_freq(k, inc=1):
    """Increase frequency up to saturating max."""
    if inc <= 0:
        return
    _freq[k] = min(_FREQ_MAX, _freq.get(k, 0) + inc)


def _record_event(kind):
    """Record one of: 'hit', 'new_miss', 'miss'."""
    global _scan_hits, _scan_new
    if kind == 'hit':
        val = 2
    elif kind == 'new_miss':
        val = 1
    else:
        val = 0
    _scan_q.append(val)
    if val == 2:
        _scan_hits += 1
    elif val == 1:
        _scan_new += 1
    # maintain window length
    while len(_scan_q) > _scan_W:
        old = _scan_q.popleft()
        if old == 2:
            _scan_hits -= 1
        elif old == 1:
            _scan_new -= 1


def _in_scan_mode():
    """Detect scans: high new-miss rate and low hit rate over recent window."""
    if not _scan_q or len(_scan_q) < _scan_W:
        return False
    hit_rate = _scan_hits / max(1, len(_scan_q))
    new_rate = _scan_new / max(1, len(_scan_q))
    return (new_rate > 0.6 and hit_rate < 0.2)


def _adjust_p(sign, cap):
    """
    Adjust ARC p (target size of T1) with momentum.
    sign > 0: increase p (favor recency, on B1 ghost hit)
    sign < 0: decrease p (favor frequency, on B2 ghost hit)
    """
    global _p, _p_mom
    if sign == 0:
        return
    this = len(_B1) if sign > 0 else len(_B2)
    other = len(_B2) if sign > 0 else len(_B1)
    ratio = other / max(1.0, float(this))
    base = max(1.0, ratio)
    step = min(base, max(1.0, cap / 4.0))
    _p_mom = 0.5 * _p_mom + (1 if sign > 0 else -1) * step
    _p = int(max(0, min(cap, _p + _p_mom)))


def _pick_from_T2(cap):
    """
    Pick a victim from T2 using small sampling of LRU end, guided by TinyLFU.
    Sample oldest few entries and evict the one with lowest frequency (tie -> oldest).
    """
    if not _T2:
        return None
    sample = []
    # Choose sample size dynamically
    t2_target = cap - int(max(0, min(cap, _p)))
    sample_size = 5 if len(_T2) > t2_target else 3
    it = iter(_T2.items())
    for _ in range(sample_size):
        try:
            k, _ = next(it)
            sample.append(k)
        except StopIteration:
            break
    if not sample:
        # fallback to LRU
        return next(iter(_T2))
    # Choose lowest freq; tie-break by position order (which is already oldest-first)
    best = None
    best_f = None
    for k in sample:
        f = _freq.get(k, 0)
        if best is None or f < best_f:
            best = k
            best_f = f
    return best


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    _init(cache_snapshot)
    _sync_segments(cache_snapshot)
    now = cache_snapshot.access_count
    cap = _cap(cache_snapshot)
    _age_frequency(now, cap)

    k_new = obj.key
    t1_size = len(_T1)
    t2_size = len(_T2)
    # ARC-informed choice of eviction source
    if k_new in _B2 and t1_size > 0:
        # Favor keeping T2 items when we saw protected ghost: evict from T1
        victim_seg = 'T1'
    elif k_new in _B1 and t2_size > 0:
        # Favor recency (grow T1) by evicting from T2
        victim_seg = 'T2'
    else:
        # Default ARC: evict from T1 if it exceeds target p, else from T2
        victim_seg = 'T1' if t1_size > int(_p) or t2_size == 0 else 'T2'

    if victim_seg == 'T1' and _T1:
        # Evict LRU from T1
        return next(iter(_T1))
    elif victim_seg == 'T2' and _T2:
        return _pick_from_T2(cap)

    # Fallback: if segments empty or desynced, evict oldest in cache
    if cache_snapshot.cache:
        return next(iter(cache_snapshot.cache.keys()))
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    _init(cache_snapshot)
    _sync_segments(cache_snapshot)
    now = cache_snapshot.access_count
    cap = _cap(cache_snapshot)
    _age_frequency(now, cap)

    k = obj.key
    _record_event('hit')
    _bump_freq(k, 1)

    in_scan = _in_scan_mode()

    if k in _T1:
        # Promotion policy: promote to T2 unless in scan-mode requiring two touches
        if in_scan and _REQUIRE_TWO_TOUCHES and _touch.get(k, 0) < 1:
            _touch[k] = _touch.get(k, 0) + 1
            # Refresh recency inside T1
            try:
                _T1.move_to_end(k, last=True)
            except KeyError:
                _T1[k] = None
        else:
            # Promote to T2 MRU
            _T1.pop(k, None)
            _T2[k] = None
            _touch[k] = 0
    elif k in _T2:
        # Refresh recency in T2
        try:
            _T2.move_to_end(k, last=True)
        except KeyError:
            _T2[k] = None
        _touch[k] = 2
    else:
        # Unknown metadata on hit: place in T2 for protection
        _T2[k] = None
        _touch[k] = 2

    # No explicit demotion here; ARC logic handles sizes via evictions and ghosts.


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    _init(cache_snapshot)
    _sync_segments(cache_snapshot)
    now = cache_snapshot.access_count
    cap = _cap(cache_snapshot)
    _age_frequency(now, cap)

    k = obj.key

    # Classify for scan detector: new miss vs ghost-revisit miss
    if k in _B1 or k in _B2:
        _record_event('miss')
    else:
        _record_event('new_miss')

    in_scan = _in_scan_mode()

    # ARC admission using ghost feedback with momentum-smoothing on p.
    if k in _B1:
        _adjust_p(+1, cap)  # favor recency (grow T1 target)
        _B1.pop(k, None)
        # In normal mode, re-admit to T2; in scan, go to T1 to reduce pollution
        if in_scan:
            _T1[k] = None
            _touch[k] = 0
            # Place at LRU in scan to get evicted quickly unless quickly reused
            try:
                _T1.move_to_end(k, last=False)
            except KeyError:
                pass
        else:
            _T2[k] = None
            _touch[k] = 2
            _bump_freq(k, 2)
    elif k in _B2:
        _adjust_p(-1, cap)  # favor frequency (shrink T1 target)
        _B2.pop(k, None)
        if in_scan:
            _T1[k] = None
            _touch[k] = 0
            try:
                _T1.move_to_end(k, last=False)
            except KeyError:
                pass
        else:
            _T2[k] = None
            _touch[k] = 2
            _bump_freq(k, 3)
    else:
        # Cold admission to T1
        if k in _T2:
            _T2.pop(k, None)
        _T1.pop(k, None)
        _T1[k] = None
        _touch[k] = 0
        if in_scan:
            # Insert at LRU under scans to minimize damage
            try:
                _T1.move_to_end(k, last=False)
            except KeyError:
                pass

    # Keep ghosts bounded
    _trim_ghosts(cap)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    if evicted_obj is None:
        return
    _init(cache_snapshot)
    cap = _cap(cache_snapshot)
    k = evicted_obj.key

    # Identify segment and move to corresponding ghost
    if k in _T1:
        _T1.pop(k, None)
        _B1[k] = None  # add MRU in ghost
    elif k in _T2:
        _T2.pop(k, None)
        _B2[k] = None
    else:
        # Unknown: assume it was in T1
        _B1[k] = None

    # Clean touch state (freq retained to help re-admission decisions)
    _touch.pop(k, None)

    # Trim ghosts to maintain bounds
    _trim_ghosts(cap)

# EVOLVE-BLOCK-END

# This part remains fixed (not evolved)
def run_caching(trace_path: str, copy_code_dst: str):
    """Run the caching algorithm on a trace"""
    import os
    with open(os.path.abspath(__file__), 'r', encoding="utf-8") as f:
        code_str = f.read()
    with open(os.path.join(copy_code_dst), 'w') as f:
        f.write(code_str)
    from cache_utils import Cache, CacheConfig, CacheObj, Trace
    trace = Trace(trace_path=trace_path)
    cache_capacity = max(int(trace.get_ndv() * 0.1), 1)
    cache = Cache(CacheConfig(cache_capacity))
    for entry in trace.entries:
        obj = CacheObj(key=str(entry.key))
        cache.get(obj)
    with open(copy_code_dst, 'w') as f:
        f.write("")
    hit_rate = round(cache.hit_count / cache.access_count, 6)
    return hit_rate