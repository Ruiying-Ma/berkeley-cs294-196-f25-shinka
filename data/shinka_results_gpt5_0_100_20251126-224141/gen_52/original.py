# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LRU timestamp map kept for compatibility and as a tie-breaker
m_key_timestamp = dict()

# Lightweight LFU counter with periodic aging
m_freq = dict()
last_age_access = 0
AGE_INTERVAL_FACTOR = 4  # age every ~4×capacity accesses (at least 500)


# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None     # will be initialized from cache_snapshot


def _ensure_capacity(cache_snapshot):
    global arc_capacity, arc_p
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)
    # Bound p within [0, C]
    if arc_capacity is not None:
        arc_p = min(max(arc_p, 0), arc_capacity)


def _maybe_age(cache_snapshot):
    global last_age_access, m_freq
    if arc_capacity is None:
        return
    interval = max(500, arc_capacity * AGE_INTERVAL_FACTOR)
    now = cache_snapshot.access_count
    if now - last_age_access >= interval:
        # Halve all frequencies to age out stale popularity
        for k in list(m_freq.keys()):
            newv = m_freq.get(k, 0) >> 1
            if newv <= 0:
                m_freq.pop(k, None)
            else:
                m_freq[k] = newv
        last_age_access = now


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
    # Keep ghosts within 2x capacity and bias trimming to track p split:
    # target |B1| ≈ p, |B2| ≈ C - p
    cap = arc_capacity if arc_capacity is not None else 1
    # Bound p
    global arc_p
    arc_p = min(max(arc_p, 0), cap)
    total_cap = 2 * cap
    while (len(arc_B1) + len(arc_B2)) > total_cap:
        target_B1 = min(cap, max(0, arc_p))
        target_B2 = max(0, cap - target_B1)
        excess_B1 = max(0, len(arc_B1) - target_B1)
        excess_B2 = max(0, len(arc_B2) - target_B2)
        if excess_B1 >= excess_B2 and arc_B1:
            _pop_lru(arc_B1)
        elif arc_B2:
            _pop_lru(arc_B2)
        else:
            # If both within target but total still exceeds (due to rounding), trim larger
            if len(arc_B1) >= len(arc_B2) and arc_B1:
                _pop_lru(arc_B1)
            elif arc_B2:
                _pop_lru(arc_B2)
            else:
                break


def _resync(cache_snapshot):
    # Ensure resident metadata tracks actual cache content and ghosts remain disjoint
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
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _pick_lfu_among_lru(od, sample_k):
    # Among the k oldest in od, pick the key with the lowest frequency.
    # Tie-break by oldest timestamp to better approximate true LRU for equals.
    if not od:
        return None
    k = max(1, sample_k)
    best_key = None
    best_freq = None
    best_ts = None
    count = 0
    for key in od.keys():
        f = m_freq.get(key, 0)
        ts = m_key_timestamp.get(key, float('inf'))
        if (best_freq is None or
            f < best_freq or
            (f == best_freq and ts < best_ts)):
            best_freq = f
            best_ts = ts
            best_key = key
        count += 1
        if count >= k:
            break
    return best_key if best_key is not None else next(iter(od))


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    # Keep metadata consistent
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    # ARC replacement: choose between T1 and T2 depending on arc_p and ghost hit type
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    choose_T1 = (t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)))

    # Frequency-aware sampling among the k oldest of the chosen list
    sample_k = min(16, max(2, (arc_capacity if arc_capacity else 1) // 8))
    candidate = None
    if choose_T1 and arc_T1:
        candidate = _pick_lfu_among_lru(arc_T1, sample_k)
    elif (not choose_T1) and arc_T2:
        candidate = _pick_lfu_among_lru(arc_T2, sample_k)

    # If preferred list empty, try the other resident list
    if candidate is None:
        if arc_T1:
            candidate = _pick_lfu_among_lru(arc_T1, sample_k)
        elif arc_T2:
            candidate = _pick_lfu_among_lru(arc_T2, sample_k)

    if candidate is None:
        # Fallback: choose the oldest by timestamp if available, else any key
        if m_key_timestamp and cache_snapshot.cache:
            min_ts = float('inf')
            best = None
            for k in cache_snapshot.cache.keys():
                ts = m_key_timestamp.get(k, float('inf'))
                if ts < min_ts:
                    min_ts = ts
                    best = k
            candidate = best
        if candidate is None and cache_snapshot.cache:
            candidate = next(iter(cache_snapshot.cache.keys()))
    return candidate


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)

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
    # Update timestamp for tie-breaking/fallback and bump frequency
    m_key_timestamp[key] = cache_snapshot.access_count
    m_freq[key] = m_freq.get(key, 0) + 1

    # Defensive: repair metadata drift if any
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, arc_p, m_freq
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    key = obj.key
    # Use boundary-aware step to prevent p overshoot
    cap = arc_capacity if arc_capacity is not None else 1
    step_cap = max(1, cap // 8)

    was_ghost = False
    # ARC admission policy
    if key in arc_B1:
        # Previously evicted from T1: favor recency by increasing p
        ratio = max(1, len(arc_B2) // max(1, len(arc_B1)))
        inc = min(ratio, step_cap, max(0, cap - arc_p))
        arc_p = min(cap, arc_p + inc)
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
        was_ghost = True
    elif key in arc_B2:
        # Previously frequent: favor frequency by decreasing p
        ratio = max(1, len(arc_B1) // max(1, len(arc_B2)))
        dec = min(ratio, step_cap, arc_p)
        arc_p = max(0, arc_p - dec)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
        was_ghost = True
    else:
        # Brand new: insert into T1 (recent)
        _move_to_mru(arc_T1, key)

    # Bound p and keep ghosts disjoint with residents and trimmed
    arc_p = min(max(arc_p, 0), cap)
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)
    _trim_ghosts()
    # Track access time; bump frequency only for ghost re-admissions (reuse signal)
    m_key_timestamp[key] = cache_snapshot.access_count
    if was_ghost:
        m_freq[key] = m_freq.get(key, 0) + 1

    # Defensive: repair metadata drift if any
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)


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
    _maybe_age(cache_snapshot)
    k = evicted_obj.key
    # Move evicted resident to corresponding ghost list and keep ghosts disjoint
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
    # Remove timestamp entry for evicted item to avoid growth (keep freq as history)
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