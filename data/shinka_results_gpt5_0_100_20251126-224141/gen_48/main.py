# EVOLVE-BLOCK-START
"""ARC-S3: Scan-guarded ARC with TinyLFU scoring and ghost-aware resync.

Resident lists:
- arc_T1 (recent), arc_T2 (frequent) as OrderedDicts for LRU order (left=LRU, right=MRU).
Ghost lists:
- arc_B1 (ghost of T1), arc_B2 (ghost of T2) as OrderedDicts for ghost LRU.

Adaptation:
- arc_p is the T1 target, updated via ghost hits with asymmetric caps and ceiling ratios.
- Idle drift nudges p toward base_w when no ghost signals occur.

Scan defense:
- During sustained cold streaks, insert cold misses at T1 LRU and demote a few T2 LRUs to T1.

Victim choice:
- ARC REPLACE (T1 vs T2) with intra-segment TinyLFU+LRU tie-break among a small LRU window.
"""

from collections import OrderedDict

# Global metadata
m_key_timestamp = dict()  # key -> last access_count (for tie-breaks/fallback)

# ARC structures
arc_T1 = OrderedDict()  # resident recent
arc_T2 = OrderedDict()  # resident frequent
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None

# Adaptation, scan, and resync state
arc_last_ghost_hit_access = 0
arc_last_ghost_hit_side = None  # 'B1' or 'B2'
cold_streak = 0

# TinyLFU-like frequency sketch with periodic decay
m_freq = dict()              # key -> decaying frequency
m_next_decay_access = None   # next access time to decay


def _ensure_capacity(cache_snapshot):
    global arc_capacity
    if arc_capacity is None:
        try:
            arc_capacity = max(int(cache_snapshot.capacity), 1)
        except Exception:
            arc_capacity = max(1, len(cache_snapshot.cache))


def _move_to_mru(od: OrderedDict, key):
    if key in od:
        od.pop(key, None)
    od[key] = True


def _move_to_lru(od: OrderedDict, key):
    if key in od:
        od.pop(key, None)
    od[key] = True
    # move to LRU position (left)
    try:
        od.move_to_end(key, last=False)
    except Exception:
        # Fallback for Py versions without move_to_end signature
        pass


def _pop_lru(od: OrderedDict):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _maybe_decay_freq(cache_snapshot):
    global m_freq, m_next_decay_access
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    period = max(8, arc_capacity or 1)
    if m_next_decay_access is None:
        m_next_decay_access = now + period
        return
    if now >= m_next_decay_access:
        if m_freq:
            for k in list(m_freq.keys()):
                newc = m_freq.get(k, 0) >> 1
                if newc:
                    m_freq[k] = newc
                else:
                    m_freq.pop(k, None)
        m_next_decay_access = now + period


def _bump_freq(key, w=1):
    try:
        inc = max(1, int(w))
    except Exception:
        inc = 1
    m_freq[key] = m_freq.get(key, 0) + inc


def _trim_ghosts():
    # Keep ghosts within 2*capacity; bias trimming opposite to last ghost hit side.
    total_limit = max(1, (arc_capacity or 1) * 2)
    while (len(arc_B1) + len(arc_B2)) > total_limit:
        if arc_last_ghost_hit_side == 'B1' and arc_B2:
            _pop_lru(arc_B2)
        elif arc_last_ghost_hit_side == 'B2' and arc_B1:
            _pop_lru(arc_B1)
        else:
            # Trim from larger side, otherwise B1
            if len(arc_B1) >= len(arc_B2):
                if not _pop_lru(arc_B1) and arc_B2:
                    _pop_lru(arc_B2)
            else:
                if not _pop_lru(arc_B2) and arc_B1:
                    _pop_lru(arc_B1)


def _resync(cache_snapshot):
    # Ensure resident metadata equals actual cache content; seed by ghost hints.
    cache_keys = set(cache_snapshot.cache.keys())
    # Remove residents not in cache
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Add missing cache keys; place in T2 if hinted by B2, else T1
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            if k in arc_B2:
                _move_to_mru(arc_T2, k)
                arc_B2.pop(k, None)
            else:
                _move_to_mru(arc_T1, k)
                arc_B1.pop(k, None)
    _trim_ghosts()


def _choose_victim_from(od: OrderedDict, consider_n: int):
    """Pick a victim among the LRU-side window using TinyLFU then recency as tie-breaker."""
    if not od:
        return None
    n = max(1, min(consider_n, len(od)))
    # Iterate first n keys from LRU side
    cnt = 0
    best_k = None
    best_score = None
    for k in od.keys():
        # ordered iteration from LRU to MRU
        f = m_freq.get(k, 0)
        ts = m_key_timestamp.get(k, 0)
        score = (f, ts)  # lower is better
        if best_score is None or score < best_score:
            best_score = score
            best_k = k
        cnt += 1
        if cnt >= n:
            break
    if best_k is not None:
        return best_k
    # Fallback LRU
    return next(iter(od))


def _idle_drift_p(now):
    # If no ghost hits for ~C accesses, nudge p toward baseline C//5 by 1 step
    global arc_p
    if arc_capacity is None:
        return
    idle = now - arc_last_ghost_hit_access
    if idle > (arc_capacity or 1):
        base_w = max(1, (arc_capacity or 1) // 5)
        if arc_p > base_w:
            arc_p -= 1
        elif arc_p < base_w:
            arc_p += 1


def _demote_t2_for_scan():
    # Temporarily demote a few T2 LRUs to T1 LRU during cold scans
    if not arc_T2:
        return
    kmax = min(2, max(1, (arc_capacity or 1) // 16))
    for _ in range(kmax):
        if not arc_T2:
            break
        k = _pop_lru(arc_T2)
        if k is None:
            break
        _move_to_lru(arc_T1, k)


def evict(cache_snapshot, obj):
    '''
    Choose eviction victim based on ARC REPLACE with TinyLFU-scored intra-segment choice.
    '''
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)

    # Enforce ghost disjointness with residents
    for k in list(arc_T1.keys()):
        arc_B1.pop(k, None)
        arc_B2.pop(k, None)
    for k in list(arc_T2.keys()):
        arc_B1.pop(k, None)
        arc_B2.pop(k, None)

    t1_sz = len(arc_T1)
    p = min(max(0, arc_p), arc_capacity or 0)
    consider_n = max(4, (arc_capacity or 1) // 16)

    # ARC REPLACE rule
    x_in_B2 = (obj.key in arc_B2)
    evict_from_t1 = (t1_sz > p) or (x_in_B2 and t1_sz >= p and t1_sz > 0)

    if evict_from_t1 and t1_sz > 0:
        victim = _choose_victim_from(arc_T1, consider_n)
        if victim is not None:
            return victim
    # Else from T2
    if arc_T2:
        victim = _choose_victim_from(arc_T2, consider_n)
        if victim is not None:
            return victim
    # Fallback to T1 if T2 empty or failed
    if arc_T1:
        return _choose_victim_from(arc_T1, consider_n)

    # Last-chance resync and fallback to oldest timestamp
    _resync(cache_snapshot)
    if arc_T1:
        return next(iter(arc_T1))
    if arc_T2:
        return next(iter(arc_T2))
    if cache_snapshot.cache:
        if m_key_timestamp:
            return min(cache_snapshot.cache.keys(), key=lambda k: m_key_timestamp.get(k, 0))
        return next(iter(cache_snapshot.cache.keys()))
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    On hit:
    - Refresh timestamp and TinyLFU.
    - Promote T1 -> T2; refresh T2 recency.
    - Idle drift of p toward baseline if no ghost hits.
    - Maintain ghost disjointness.
    '''
    global cold_streak
    _ensure_capacity(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    now = cache_snapshot.access_count
    _idle_drift_p(now)

    key = obj.key
    cold_streak = 0  # any hit breaks cold streak

    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    elif key in arc_T2:
        _move_to_mru(arc_T2, key)
    else:
        # Drift recovery: place unknown resident as recent
        _move_to_mru(arc_T1, key)

    # Maintain disjointness
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    # Update timestamp and frequency
    m_key_timestamp[key] = now
    _bump_freq(key, 2)

    _trim_ghosts()


def update_after_insert(cache_snapshot, obj):
    '''
    On insert:
    - Ghost-driven adaptation of p with asymmetric caps using ceiling ratios.
    - On B1/B2 hit: insert into T2 (protected).
    - On cold miss: insert into T1; under sustained cold streak, insert at T1 LRU and demote a few T2 LRUs.
    '''
    global arc_p, arc_last_ghost_hit_access, arc_last_ghost_hit_side, cold_streak
    _ensure_capacity(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    now = cache_snapshot.access_count
    key = obj.key
    cap = arc_capacity or 1

    in_B1 = key in arc_B1
    in_B2 = key in arc_B2

    inc_cap = max(1, cap // 8)
    dec_cap = max(1, (cap // 4) if cold_streak >= max(1, cap // 2) else (cap // 8))

    if in_B1:
        # Increase p (favor recency)
        denom = max(1, len(arc_B1))
        numer = len(arc_B2)
        raw_inc = max(1, (numer + denom - 1) // denom)  # ceil(|B2|/|B1|)
        arc_p = min(cap, arc_p + min(inc_cap, raw_inc))
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)  # protect on ghost hit
        arc_last_ghost_hit_access = now
        arc_last_ghost_hit_side = 'B1'
        cold_streak = 0
        _bump_freq(key, 3)
    elif in_B2:
        # Decrease p (favor frequency)
        denom = max(1, len(arc_B2))
        numer = len(arc_B1)
        raw_dec = max(1, (numer + denom - 1) // denom)  # ceil(|B1|/|B2|)
        arc_p = max(0, arc_p - min(dec_cap, raw_dec))
        arc_B2.pop(key, None)
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)  # protect on ghost hit
        arc_last_ghost_hit_access = now
        arc_last_ghost_hit_side = 'B2'
        cold_streak = 0
        _bump_freq(key, 4)
    else:
        # Cold miss
        cold_streak += 1
        if cold_streak >= max(1, cap // 2):
            # Insert at T1 LRU to be easily evicted if it's part of a scan
            _move_to_lru(arc_T1, key)
            _demote_t2_for_scan()
        else:
            _move_to_mru(arc_T1, key)
        # Gentle clamp during long streaks
        if cold_streak % max(1, cap // 2) == 0:
            arc_p = max(0, arc_p - max(1, cap // 16))
        _bump_freq(key, 1)

    # Maintain disjointness
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    m_key_timestamp[key] = now
    _trim_ghosts()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After eviction:
    - Move evicted resident to B1 if from T1 else to B2 if from T2.
    - Keep ghosts disjoint and bounded.
    '''
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key

    if k in arc_T1:
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
        arc_B2.pop(k, None)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
        arc_B1.pop(k, None)
    else:
        # Unknown: default to B1 unless it already exists in B2
        if k in arc_B2:
            _move_to_mru(arc_B2, k)
            arc_B1.pop(k, None)
        else:
            _move_to_mru(arc_B1, k)
            arc_B2.pop(k, None)

    # Clean up timestamps to avoid stale memory
    m_key_timestamp.pop(k, None)
    _trim_ghosts()
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