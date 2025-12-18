# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LRU timestamp map kept for tie-breaking and fallback
m_key_timestamp = dict()

# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1 (recent history)
arc_B2 = OrderedDict()  # ghost of T2 (frequent history)
arc_p = 0               # target size of T1
arc_capacity = None     # initialized from cache_snapshot

# Idle tracking for gentle scan recovery
last_ghost_hit_access = -1  # last access_count when B1/B2 was hit
# Track when p was last updated to avoid double updates (evict + insert in same access)
last_p_update_access = -1


def _ensure_capacity(cache_snapshot):
    global arc_capacity, arc_p
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)
    # Clamp p into [0, capacity]
    if arc_capacity is not None:
        arc_p = min(max(arc_p, 0), arc_capacity)


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
    # Keep ghosts total size within 2x capacity and bias trimming toward p split
    cap = arc_capacity if arc_capacity is not None else 1
    # Bound p
    global arc_p
    arc_p = min(max(arc_p, 0), cap)
    total_cap = 2 * cap

    # Proportional targets guided by p within [0, C]
    target_B1 = min(cap, max(0, arc_p))
    target_B2 = max(0, cap - target_B1)

    # Enforce total size first
    while (len(arc_B1) + len(arc_B2)) > total_cap:
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
    # Ensure resident metadata tracks actual cache content
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Any cached keys not tracked: assume recent (T1)
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            arc_T1[k] = True
    # Keep ghosts disjoint from residents (robustness)
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _decay_p_if_idle(cache_snapshot):
    # If no ghost hits for a while, gently and proportionally decay p toward 0
    global arc_p
    if last_ghost_hit_access >= 0:
        idle = cache_snapshot.access_count - last_ghost_hit_access
        if idle > 0 and arc_p > 0:
            cap = arc_capacity if arc_capacity else 1
            # Proportional, bounded decay to hasten recovery after long idle
            step = max(1, idle // max(1, cap // 4))
            arc_p = max(0, arc_p - min(cap // 8, step))


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global arc_p, last_ghost_hit_access, last_p_update_access
    _ensure_capacity(cache_snapshot)
    # Keep metadata consistent first and decay p if idle
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    # If the request is a ghost hit, update p before REPLACE (canonical ARC)
    step_cap = max(1, arc_capacity // 8)
    key = obj.key
    if key in arc_B1:
        # Favor recency: increase p
        inc = max(1, len(arc_B2) // max(1, len(arc_B1)))
        inc = min(inc, step_cap, max(0, arc_capacity - arc_p))
        arc_p = min(arc_capacity, arc_p + inc)
        last_ghost_hit_access = cache_snapshot.access_count
        last_p_update_access = cache_snapshot.access_count
    elif key in arc_B2:
        # Favor frequency: decrease p
        dec = max(1, len(arc_B1) // max(1, len(arc_B2)))
        dec = min(dec, step_cap, arc_p)
        arc_p = max(0, arc_p - dec)
        last_ghost_hit_access = cache_snapshot.access_count
        last_p_update_access = cache_snapshot.access_count

    # ARC replacement: choose between T1 and T2 depending on arc_p and ghost hit type
    x_in_B2 = key in arc_B2
    t1_sz = len(arc_T1)
    from_t1 = (t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)))

    # Primary choice
    if from_t1 and arc_T1:
        candidate = next(iter(arc_T1))
    elif (not from_t1) and arc_T2:
        candidate = next(iter(arc_T2))
    elif arc_T1:
        candidate = next(iter(arc_T1))
    elif arc_T2:
        candidate = next(iter(arc_T2))
    else:
        candidate = None

    # Try to repair metadata and retry ARC replacement before falling back
    if candidate is None or candidate not in cache_snapshot.cache:
        _resync(cache_snapshot)
        t1_sz = len(arc_T1)
        from_t1 = (t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)))
        if from_t1 and arc_T1:
            candidate = next(iter(arc_T1))
        elif arc_T2:
            candidate = next(iter(arc_T2))

    # Ghost-informed fallback: prefer evicting something present in B1 (recency-only)
    if candidate is None or candidate not in cache_snapshot.cache:
        for k in cache_snapshot.cache.keys():
            if k in arc_B1:
                candidate = k
                break
    # Next: prefer any key not in B2 (avoid evicting likely frequent)
    if (candidate is None or candidate not in cache_snapshot.cache) and cache_snapshot.cache:
        for k in cache_snapshot.cache.keys():
            if k not in arc_B2:
                candidate = k
                break
    # Otherwise, timestamp tie-breaker
    if (candidate is None or candidate not in cache_snapshot.cache) and cache_snapshot.cache:
        if m_key_timestamp:
            min_ts = min(m_key_timestamp.get(k, float('inf')) for k in cache_snapshot.cache.keys())
            for k in cache_snapshot.cache.keys():
                if m_key_timestamp.get(k, float('inf')) == min_ts:
                    candidate = k
                    break
    # Last resort: arbitrary
    if (candidate is None or candidate not in cache_snapshot.cache) and cache_snapshot.cache:
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
    # Resident keys must not exist in ghosts
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)
    # Update timestamp for tie-breaking/fallback
    m_key_timestamp[key] = cache_snapshot.access_count

    # Post-condition: keep metadata consistent
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
    global m_key_timestamp, arc_p, last_ghost_hit_access, last_p_update_access
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    key = obj.key
    # ARC admission policy with bounded p updates
    step_cap = max(1, arc_capacity // 8)

    if key in arc_B1 or key in arc_B2:
        # If evict already adapted p this access, don't double-step
        if last_p_update_access != cache_snapshot.access_count:
            if key in arc_B1:
                inc = max(1, len(arc_B2) // max(1, len(arc_B1)))
                inc = min(inc, step_cap, max(0, arc_capacity - arc_p))
                arc_p = min(arc_capacity, arc_p + inc)
            else:
                dec = max(1, len(arc_B1) // max(1, len(arc_B2)))
                dec = min(dec, step_cap, arc_p)
                arc_p = max(0, arc_p - dec)
            last_ghost_hit_access = cache_snapshot.access_count
            last_p_update_access = cache_snapshot.access_count
        # Admission to T2 for ghost reuses
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1 (recent)
        _move_to_mru(arc_T1, key)
        # Ensure ghosts are disjoint from residents
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)

    _trim_ghosts()
    m_key_timestamp[key] = cache_snapshot.access_count

    # Post-condition: keep metadata consistent
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
    k = evicted_obj.key
    # Move evicted resident to corresponding ghost list
    if k in arc_T1:
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
        arc_B2.pop(k, None)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
        arc_B1.pop(k, None)
    else:
        # Unknown membership: prefer existing ghost membership if any (favor B2), else default to B1
        if k in arc_B2:
            _move_to_mru(arc_B2, k)
            arc_B1.pop(k, None)
        else:
            _move_to_mru(arc_B1, k)
            arc_B2.pop(k, None)
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