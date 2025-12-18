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
# Scan detection counter: consecutive brand-new inserts (no ghost)
cold_streak = 0


def _ensure_capacity(cache_snapshot):
    global arc_capacity
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)


def _move_to_mru(od, key):
    # Push key to MRU position of an OrderedDict
    if key in od:
        od.pop(key, None)
    od[key] = True

def _insert_at_lru(od, key):
    # Insert key at LRU position (probation)
    if key in od:
        od.pop(key, None)
    od[key] = True
    try:
        # Move to beginning (LRU side)
        od.move_to_end(key, last=False)
    except Exception:
        # Fallback: ignore if move_to_end isn't available
        pass


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _trim_ghosts():
    # Keep ghosts total size within capacity (matches best baseline)
    total = len(arc_B1) + len(arc_B2)
    cap = arc_capacity if arc_capacity is not None else 1
    while total > cap:
        # Evict from the larger ghost list first
        if len(arc_B1) >= len(arc_B2):
            _pop_lru(arc_B1)
        else:
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
    # Any cached keys not tracked: seed using ghost hints for better accuracy
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            if k in arc_B2:
                _move_to_mru(arc_T2, k)
                arc_B2.pop(k, None)
            elif k in arc_B1:
                _move_to_mru(arc_T1, k)
                arc_B1.pop(k, None)
            else:
                _move_to_mru(arc_T1, k)
    # Keep ghosts disjoint from residents (robustness)
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _decay_p_if_idle(cache_snapshot):
    # If no ghost hits for a while, gently decay p toward 0 to recover from scans
    global arc_p
    if last_ghost_hit_access >= 0:
        idle = cache_snapshot.access_count - last_ghost_hit_access
        if idle > (arc_capacity if arc_capacity else 1) and arc_p > 0:
            arc_p = max(0, arc_p - 1)


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
    _decay_p_if_idle(cache_snapshot)

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

    if candidate is None:
        # Ghost-informed fallback:
        # 1) Prefer evicting a cached key present in B1 (recency-only history)
        for k in cache_snapshot.cache.keys():
            if k in arc_B1:
                candidate = k
                break
        # 2) Otherwise prefer any key not hinted as frequent (not in B2)
        if candidate is None:
            for k in cache_snapshot.cache.keys():
                if k not in arc_B2:
                    candidate = k
                    break
        # 3) Timestamp tie-breaker
        if candidate is None and m_key_timestamp:
            min_ts = min(m_key_timestamp.get(k, float('inf')) for k in cache_snapshot.cache.keys())
            for k in cache_snapshot.cache.keys():
                if m_key_timestamp.get(k, float('inf')) == min_ts:
                    candidate = k
                    break
        # 4) Last resort: arbitrary
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
    global m_key_timestamp, cold_streak
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
    # Any hit breaks a cold streak
    cold_streak = 0
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
    global m_key_timestamp, arc_p, last_ghost_hit_access, cold_streak
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    key = obj.key
    # ARC admission policy with capped p updates and scan-aware handling
    if key in arc_B1:
        # Previously evicted from T1: favor recency by increasing p (capped)
        inc_ratio = max(1, len(arc_B2) // max(1, len(arc_B1)))
        inc_cap = max(1, arc_capacity // 8)
        arc_p = min(arc_capacity, arc_p + min(inc_ratio, inc_cap))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)  # keep ghosts disjoint
        _move_to_mru(arc_T2, key)
    elif key in arc_B2:
        # Previously frequent: favor frequency by decreasing p (asymmetric cap under scans)
        dec_ratio = max(1, len(arc_B1) // max(1, len(arc_B2)))
        scan_threshold = max(1, arc_capacity // 2)
        dec_cap = max(1, (arc_capacity // 4) if cold_streak >= scan_threshold else (arc_capacity // 8))
        arc_p = max(0, arc_p - min(dec_ratio, dec_cap))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        arc_B2.pop(key, None)
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1; during scans, insert at LRU to reduce pollution and push p down
        cold_streak += 1
        if cold_streak >= max(1, arc_capacity // 2):
            _insert_at_lru(arc_T1, key)
            # Accelerate recovery from frequency bias during scans
            arc_p = max(0, arc_p - max(1, arc_capacity // 8))
        else:
            _move_to_mru(arc_T1, key)
        # Ensure ghosts are disjoint from residents
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)

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
        # Unknown membership: default to B1
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