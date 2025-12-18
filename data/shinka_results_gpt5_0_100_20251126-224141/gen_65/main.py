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


def _ensure_capacity(cache_snapshot):
    global arc_capacity
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)


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
    # Keep ghosts total size within 2x capacity with proportional bias toward targets
    cap = arc_capacity if arc_capacity is not None else 1
    target_B1 = min(cap, max(0, arc_p))
    target_B2 = max(0, cap - target_B1)
    limit = 2 * cap
    total = len(arc_B1) + len(arc_B2)
    while total > limit:
        excess_B1 = max(0, len(arc_B1) - target_B1)
        excess_B2 = max(0, len(arc_B2) - target_B2)
        if excess_B1 >= excess_B2 and arc_B1:
            _pop_lru(arc_B1)
        elif arc_B2:
            _pop_lru(arc_B2)
        else:
            # If both within targets but still exceeding total, trim the larger
            if len(arc_B1) >= len(arc_B2) and arc_B1:
                _pop_lru(arc_B1)
            elif arc_B2:
                _pop_lru(arc_B2)
        total = len(arc_B1) + len(arc_B2)


def _resync(cache_snapshot):
    # Ensure resident metadata tracks actual cache content
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
    # Ghosts must be disjoint from residents
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
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
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    # ARC replacement: choose between T1 and T2 depending on arc_p and ghost hit type
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    candidate = None
    if t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)):
        # Evict LRU from T1
        candidate = next(iter(arc_T1)) if arc_T1 else None
    else:
        # Evict LRU from T2
        candidate = next(iter(arc_T2)) if arc_T2 else None
    # If preferred list is empty, try the other resident list explicitly
    if candidate is None:
        if arc_T1:
            candidate = next(iter(arc_T1))
        elif arc_T2:
            candidate = next(iter(arc_T2))
    if candidate is None:
        # Fallback: choose the oldest by timestamp if available, else any key
        if m_key_timestamp:
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
    # ARC: on hit, move to T2 MRU
    key = obj.key
    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    elif key in arc_T2:
        _move_to_mru(arc_T2, key)
    else:
        # If not present due to drift, place in T2 (protect frequent)
        _move_to_mru(arc_T2, key)
    # Resident keys must not exist in ghosts
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)
    # Update timestamp for tie-breaking/fallback
    m_key_timestamp[key] = cache_snapshot.access_count


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, arc_p
    _ensure_capacity(cache_snapshot)
    key = obj.key
    # ARC admission policy with bounded p updates and disjoint ghosts
    step_cap = max(1, (arc_capacity if arc_capacity is not None else 1) // 8)
    if key in arc_B1:
        # Previously evicted from T1: favor recency by increasing p
        inc = max(1, len(arc_B2) // max(1, len(arc_B1)))
        inc = min(inc, step_cap, max(0, (arc_capacity if arc_capacity else 1) - arc_p))
        arc_p = min(arc_capacity, arc_p + inc)
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
    elif key in arc_B2:
        # Previously frequent: favor frequency by decreasing p
        dec = max(1, len(arc_B1) // max(1, len(arc_B2)))
        dec = min(dec, step_cap, arc_p)
        arc_p = max(0, arc_p - dec)
        arc_B2.pop(key, None)
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1 (recent)
        _move_to_mru(arc_T1, key)
        # Ensure ghosts are disjoint from residents
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
    # Bound p and trim ghosts
    arc_p = min(max(arc_p, 0), arc_capacity if arc_capacity is not None else arc_p)
    _trim_ghosts()
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
    # Move evicted resident to corresponding ghost list; maintain disjointness
    if k in arc_T1:
        arc_T1.pop(k, None)
        arc_B2.pop(k, None)
        _move_to_mru(arc_B1, k)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        arc_B1.pop(k, None)
        _move_to_mru(arc_B2, k)
    else:
        # Unknown membership: prefer existing ghost presence (favor B2 if present), else default to B1
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