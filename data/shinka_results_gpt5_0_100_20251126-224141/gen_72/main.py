# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LRU timestamp map kept for compatibility and as a tie-breaker
m_key_timestamp = dict()

# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None     # will be initialized from cache_snapshot

# Scan and adaptation trackers
last_ghost_hit_access = -1  # last access_count when B1/B2 was hit
cold_streak = 0             # consecutive brand-new insertions
scan_guard_until = -1       # time until which we bias REPLACE to protect T2


def _ensure_capacity(cache_snapshot):
    global arc_capacity, arc_p
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)
    # Clamp p to [0, C]
    arc_p = max(0, min(arc_p, arc_capacity))


def _decay_p_if_idle(cache_snapshot):
    # Gentle decay of p toward 0 when no ghost hits have happened recently.
    # Helps recover from scans/uniform phases without oscillation.
    global arc_p
    cap = arc_capacity if arc_capacity is not None else 1
    idle = 0 if last_ghost_hit_access < 0 else (cache_snapshot.access_count - last_ghost_hit_access)
    if idle > 0 and cap > 0:
        # Proportional decay bounded by cap/8 per call
        step = max(1, idle // max(1, cap // 4))
        arc_p = max(0, arc_p - min(max(1, cap // 8), step))
        # Extra clamp during extended cold streaks
        if cold_streak >= max(1, cap // 2):
            arc_p = max(0, arc_p - min(max(1, cap // 4), cold_streak // max(1, cap // 8)))


def _move_to_mru(od, key):
    # Push key to MRU position of an OrderedDict
    if key in od:
        od.pop(key, None)
    od[key] = True


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _trim_ghosts():
    # Keep ghosts total size within capacity, prefer trimming the side that exceeds
    # its per-side target: target_B1 ≈ p, target_B2 ≈ C - p
    cap = arc_capacity if arc_capacity is not None else 1
    # Clamp p to [0, C]
    global arc_p
    arc_p = max(0, min(arc_p, cap))
    total = len(arc_B1) + len(arc_B2)
    while total > cap:
        target_B1 = min(cap, max(0, arc_p))
        target_B2 = max(0, cap - target_B1)
        excess_B1 = max(0, len(arc_B1) - target_B1)
        excess_B2 = max(0, len(arc_B2) - target_B2)
        if excess_B1 >= excess_B2 and arc_B1:
            _pop_lru(arc_B1)
        elif arc_B2:
            _pop_lru(arc_B2)
        else:
            # If both within target but total still exceeds, trim the larger
            if len(arc_B1) >= len(arc_B2) and arc_B1:
                _pop_lru(arc_B1)
            elif arc_B2:
                _pop_lru(arc_B2)
            else:
                break
        total = len(arc_B1) + len(arc_B2)


def _resync(cache_snapshot):
    # Ensure resident metadata tracks actual cache content and keep ghosts disjoint
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Add any cached keys we missed to T1 as recent
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            arc_T1[k] = True
    # Ghosts must not contain residents
    for k in list(arc_B1.keys()):
        if k in cache_keys or k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in cache_keys or k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global arc_p, last_ghost_hit_access, cold_streak
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    cap = arc_capacity if arc_capacity is not None else 1

    # Consolidate ghost-driven p updates here to avoid double-stepping
    if obj.key in arc_B1 or obj.key in arc_B2:
        if obj.key in arc_B1:
            # Increase p toward recency
            num = len(arc_B2)
            den = max(1, len(arc_B1))
            delta = (num + den - 1) // den  # ceil
            arc_p = min(cap, arc_p + min(delta, max(1, cap // 8)))
        else:
            # Decrease p toward frequency, with stronger step under long cold streaks
            num = len(arc_B1)
            den = max(1, len(arc_B2))
            delta = (num + den - 1) // den  # ceil
            step_bound = max(1, cap // 4) if cold_streak >= max(1, cap // 2) else max(1, cap // 8)
            dec = min(delta, step_bound, arc_p)
            arc_p = max(0, arc_p - dec)
        # Record ghost-hit time and reset scan indicators
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0

    # Apply scan guard bias window if active for new insertions
    effective_p = arc_p
    if cache_snapshot.access_count <= scan_guard_until:
        effective_p = max(0, arc_p - max(1, cap // 8))

    # Canonical ARC REPLACE decision with effective p
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    from_t1 = (t1_sz >= 1 and (t1_sz > effective_p or (x_in_B2 and t1_sz == effective_p)))

    # Primary choice
    if from_t1 and arc_T1:
        return next(iter(arc_T1))
    if (not from_t1) and arc_T2:
        return next(iter(arc_T2))

    # Try the other list if preferred is empty
    if arc_T1:
        return next(iter(arc_T1))
    if arc_T2:
        return next(iter(arc_T2))

    # Resync and retry once if both were empty
    _resync(cache_snapshot)
    t1_sz = len(arc_T1)
    from_t1 = (t1_sz >= 1 and (t1_sz > effective_p or (x_in_B2 and t1_sz == effective_p)))
    if from_t1 and arc_T1:
        return next(iter(arc_T1))
    if (not from_t1) and arc_T2:
        return next(iter(arc_T2))

    # Deterministic ghost-informed fallback
    # (a) T1 LRU not in B2 (avoid evicting likely frequent)
    for k in arc_T1.keys():
        if k not in arc_B2:
            return k
    # (b) T2 LRU that appears in B1 (recency-only on T2 not proven frequent)
    for k in arc_T2.keys():
        if k in arc_B1:
            return k
    # (c) Depth-limited peek for a non-B2 key
    depth = max(1, min(8, cap // 16))
    cnt = 0
    for k in arc_T1.keys():
        if k not in arc_B2:
            return k
        cnt += 1
        if cnt >= depth:
            break
    cnt = 0
    for k in arc_T2.keys():
        if k in arc_B1:
            return k
        cnt += 1
        if cnt >= depth:
            break
    # (d) Timestamp tie-breaker restricted to T1, else any
    if arc_T1 and m_key_timestamp:
        min_ts = float('inf')
        best = None
        for k in arc_T1.keys():
            ts = m_key_timestamp.get(k, float('inf'))
            if ts < min_ts:
                min_ts = ts
                best = k
        if best is not None:
            return best
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
    global m_key_timestamp, cold_streak, scan_guard_until
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    # ARC: on hit, move to T2 MRU
    key = obj.key
    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # If already in T2, refresh; if not present due to drift, place in T2
        if key in arc_T2:
            _move_to_mru(arc_T2, key)
        else:
            _move_to_mru(arc_T2, key)
    # Keep ghosts disjoint with residents
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)
    # Update timestamp and reset scan indicators
    m_key_timestamp[key] = cache_snapshot.access_count
    cold_streak = 0
    scan_guard_until = -1


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, last_ghost_hit_access, cold_streak, scan_guard_until
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    key = obj.key
    cap = arc_capacity if arc_capacity is not None else 1

    # ARC admission policy without p updates (handled in evict)
    if key in arc_B1:
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
    elif key in arc_B2:
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
    else:
        # Brand new: insert into T1 (recent) and consider scan guard
        _move_to_mru(arc_T1, key)
        cold_streak += 1
        if cold_streak >= max(1, cap // 2):
            scan_guard_until = cache_snapshot.access_count + max(1, cap // 8)

    # Ensure ghosts disjoint and trimmed
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)
    _trim_ghosts()
    # Timestamp for tie-breaker
    m_key_timestamp[key] = cache_snapshot.access_count


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key
    # Move evicted resident to corresponding ghost list, keep ghosts disjoint
    if k in arc_T1:
        arc_T1.pop(k, None)
        arc_B2.pop(k, None)
        _move_to_mru(arc_B1, k)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        arc_B1.pop(k, None)
        _move_to_mru(arc_B2, k)
    else:
        # Unknown membership: prefer consistency with existing ghost presence
        if k in arc_B2:
            arc_B1.pop(k, None)
            _move_to_mru(arc_B2, k)
        else:
            arc_B2.pop(k, None)
            _move_to_mru(arc_B1, k)
    # Remove timestamp entry for evicted item to avoid growth
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