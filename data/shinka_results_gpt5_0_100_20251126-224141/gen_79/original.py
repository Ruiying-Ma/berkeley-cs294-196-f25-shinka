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

# Scan/ghost tracking for adaptive p and pollution control
last_ghost_hit_access = -1  # last time a B1/B2 ghost was hit
cold_streak = 0             # consecutive brand-new requests (no ghost signal)


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
    # Keep ghosts total size within capacity
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
    # Add any cached keys we missed; use ghosts to seed lists more accurately
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            if k in arc_B2:
                _move_to_mru(arc_T2, k)
                arc_B2.pop(k, None)
            elif k in arc_B1:
                _move_to_mru(arc_T1, k)
                arc_B1.pop(k, None)
            else:
                arc_T1[k] = True
    # Keep ghosts disjoint from residents
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _decay_p_if_idle(cache_snapshot):
    # If no ghost hits for a while, gently decay p toward 0 to recover from scans
    global arc_p, last_ghost_hit_access
    if last_ghost_hit_access >= 0:
        idle = cache_snapshot.access_count - last_ghost_hit_access
        C = arc_capacity if arc_capacity is not None else 1
        if idle > C and arc_p > 0:
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
    global arc_p, last_ghost_hit_access, cold_streak
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    # Ghost-driven p updates BEFORE REPLACE (canonical ARC flow)
    key = obj.key
    C = arc_capacity if arc_capacity is not None else 1
    in_B1 = key in arc_B1
    in_B2 = key in arc_B2
    if in_B1:
        step = max(1, len(arc_B2) // max(1, len(arc_B1)))
        arc_p = min(C, arc_p + min(step, max(1, C // 8)))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
    elif in_B2:
        step = max(1, len(arc_B1) // max(1, len(arc_B2)))
        arc_p = max(0, arc_p - min(step, max(1, C // 8)))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
    else:
        # Brand-new key: increase cold streak; mildly bias p downward under scans
        cold_streak += 1
        if cold_streak >= max(1, C // 2):
            arc_p = max(0, arc_p - max(1, C // 8))

    # ARC REPLACE with light scan bias: prefer T1 evictions during scan streaks
    t1_sz = len(arc_T1)
    candidate = None
    if cold_streak >= max(1, C // 2) and t1_sz > 0:
        candidate = next(iter(arc_T1))
    else:
        if t1_sz >= 1 and (t1_sz > arc_p or (in_B2 and t1_sz == arc_p)):
            # Evict LRU from T1
            candidate = next(iter(arc_T1)) if arc_T1 else None
        else:
            # Evict LRU from T2
            candidate = next(iter(arc_T2)) if arc_T2 else None

    # Strengthened, ghost-informed fallback selection when chosen list is empty
    if candidate is None:
        # 1) Prefer T1 LRU not hinted as frequent (i.e., not in B2)
        for k in list(arc_T1.keys()):
            if k not in arc_B2:
                candidate = k
                break
    if candidate is None:
        # 2) Prefer T2 LRU that shows up in B1 (recency-only hint)
        for k in list(arc_T2.keys()):
            if k in arc_B1:
                candidate = k
                break
    if candidate is None:
        # 3) Small-budget scan to avoid B2-hinted keys
        budget = max(1, C // 16)
        cnt = 0
        for k in arc_T1.keys():
            if k not in arc_B2:
                candidate = k
                break
            cnt += 1
            if cnt >= budget:
                break
        if candidate is None:
            cnt = 0
            for k in arc_T2.keys():
                if k in arc_B1:
                    candidate = k
                    break
                cnt += 1
                if cnt >= budget:
                    break

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
    global m_key_timestamp, cold_streak
    _ensure_capacity(cache_snapshot)
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
    global m_key_timestamp
    _ensure_capacity(cache_snapshot)
    key = obj.key
    # ARC admission policy: ghost hits go to T2 (p already adjusted in evict)
    if key in arc_B1 or key in arc_B2:
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)  # keep ghosts disjoint
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1 (recent)
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
        # Unknown membership: default to B1
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