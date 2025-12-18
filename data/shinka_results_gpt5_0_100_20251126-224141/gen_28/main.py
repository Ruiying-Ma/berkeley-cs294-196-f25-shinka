# EVOLVE-BLOCK-START
"""Adaptive Replacement Cache (ARC) to optimize hit rates across diverse workloads"""
from collections import OrderedDict

# In-cache lists (LRU order: left=LRU, right=MRU)
_T1 = OrderedDict()  # Recent (seen once)
_T2 = OrderedDict()  # Frequent (seen >= twice)

# Ghost lists (track recently evicted keys from T1/T2; no data, just keys)
_B1 = OrderedDict()
_B2 = OrderedDict()

# Target size for T1 (recency portion). Adapted online.
_p = 0.0

# Observed capacity (number of items). We align with cache_snapshot.capacity.
_g_capacity = 0


def _ensure_capacity(cache_snapshot):
    """Initialize/refresh capacity-aware parameters and trim ghost lists."""
    global _g_capacity, _p
    if cache_snapshot is None:
        return
    cap = cache_snapshot.capacity
    # Fallback to at least 1; also ensure not below current cache length
    cap = max(cap, len(cache_snapshot.cache), 1)
    if _g_capacity != cap:
        _g_capacity = cap
        # Initialize p to half capacity on first run; clamp otherwise
        if _p == 0.0:
            _p = float(max(1, int(round(0.5 * _g_capacity))))
        # Clamp p into [0, C] whenever capacity changes
        _p = min(max(_p, 0.0), float(_g_capacity))

    # Keep ghosts bounded and disjoint using proportional 2C policy
    _trim_ghosts()


def _trim_ghosts():
    """Maintain ghost lists:
    - Disjoint from residents
    - Total size <= 2 * capacity
    - Proportional trimming toward targets |B1|≈p and |B2|≈C-p
    """
    # Remove any resident keys from ghosts (disjointness)
    for rk in list(_T1.keys()):
        _B1.pop(rk, None)
        _B2.pop(rk, None)
    for rk in list(_T2.keys()):
        _B1.pop(rk, None)
        _B2.pop(rk, None)

    total_cap = 2 * max(_g_capacity, 1)
    t1_target = int(round(min(max(_p, 0.0), float(_g_capacity))))
    b1_target = t1_target
    b2_target = max(_g_capacity - t1_target, 0)

    # First ensure combined size bound
    while (len(_B1) + len(_B2)) > total_cap:
        over1 = len(_B1) - b1_target
        over2 = len(_B2) - b2_target
        if over1 >= over2:
            if _B1:
                _B1.popitem(last=False)
            elif _B2:
                _B2.popitem(last=False)
        else:
            if _B2:
                _B2.popitem(last=False)
            elif _B1:
                _B1.popitem(last=False)

    # Light proportional correction even if under total bound
    while len(_B1) > max(b1_target, _g_capacity):
        _B1.popitem(last=False)
    while len(_B2) > max(b2_target, _g_capacity):
        _B2.popitem(last=False)


def _resync(cache_snapshot):
    """Rebuild resident metadata from actual cache when desynchronized."""
    if cache_snapshot is None:
        return
    residents = set(cache_snapshot.cache.keys())

    # Purge stale entries from T1/T2
    for k in list(_T1.keys()):
        if k not in residents:
            _T1.pop(k, None)
    for k in list(_T2.keys()):
        if k not in residents:
            _T2.pop(k, None)

    # Add missing residents into T1 MRU conservatively
    for k in residents:
        if k not in _T1 and k not in _T2:
            _move_to_mru(_T1, k)

    # Keep ghosts disjoint and bounded
    for k in residents:
        _B1.pop(k, None)
        _B2.pop(k, None)
    _trim_ghosts()


def _move_to_mru(od: OrderedDict, key: str):
    """Place key at MRU position of the ordered dict."""
    if key in od:
        od.pop(key, None)
    od[key] = None


def _lru_key(od: OrderedDict):
    """Get LRU key without removing; None if empty."""
    try:
        return next(iter(od))
    except StopIteration:
        return None


def evict(cache_snapshot, obj):
    '''
    Choose eviction victim using strict ARC replace() logic:
    - Evict from T1 if |T1| > p, or (obj in B2 and |T1| == p)
    - Else evict from T2
    Robust fallback: resync metadata and retry before last-resort pick.
    '''
    _ensure_capacity(cache_snapshot)

    # Keep ghosts disjoint from residents
    for rk in list(_T1.keys()):
        _B1.pop(rk, None)
        _B2.pop(rk, None)
    for rk in list(_T2.keys()):
        _B1.pop(rk, None)
        _B2.pop(rk, None)

    if (len(_T1) + len(_T2)) == 0 and len(cache_snapshot.cache) > 0:
        _resync(cache_snapshot)

    p_int = int(round(min(max(_p, 0.0), float(_g_capacity))))
    t1_sz = len(_T1)
    t2_sz = len(_T2)

    victim = None
    choose_t1 = False
    if t1_sz > 0 and (t1_sz > p_int or (obj.key in _B2 and t1_sz >= p_int)):
        choose_t1 = True

    if choose_t1:
        victim = _lru_key(_T1) if _T1 else (_lru_key(_T2) if _T2 else None)
    else:
        victim = _lru_key(_T2) if _T2 else (_lru_key(_T1) if _T1 else None)

    # Fallback: resync and retry, then last-resort pick from cache
    if victim is None or victim not in cache_snapshot.cache:
        _resync(cache_snapshot)
        victim = _lru_key(_T1) if _T1 else _lru_key(_T2)
        if victim is None or victim not in cache_snapshot.cache:
            for k in cache_snapshot.cache:
                victim = k
                break

    return victim


def update_after_hit(cache_snapshot, obj):
    '''
    Update ARC state after a cache hit.
    - If hit in T1: promote to T2 MRU.
    - If hit in T2: move to T2 MRU.
    - If not tracked (desync), consider it frequent and add to T2 MRU.
    Also keep ghost lists disjoint and trim ghosts.
    '''
    _ensure_capacity(cache_snapshot)
    key = obj.key

    # Keep sets disjoint
    _B1.pop(key, None)
    _B2.pop(key, None)

    if key in _T1:
        _T1.pop(key, None)
        _move_to_mru(_T2, key)
    elif key in _T2:
        _move_to_mru(_T2, key)
    else:
        # Metadata desync: treat as frequent since it hit
        _move_to_mru(_T2, key)

    # Proactively resync if sizes drift
    if (len(_T1) + len(_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    _trim_ghosts()


def update_after_insert(cache_snapshot, obj):
    '''
    Update ARC state after inserting a new object (on miss).
    - Default: insert into T1 MRU (recency).
    - If the key is found in B1 (ghost of T1): increase p and insert into T2 MRU.
    - If the key is found in B2 (ghost of T2): decrease p and insert into T2 MRU.
    Adaptation steps are capped to avoid overshoot. Ghost lists stay disjoint and bounded.
    '''
    _ensure_capacity(cache_snapshot)
    key = obj.key
    global _p

    # If metadata already had it in cache lists, treat as hit
    if key in _T1:
        _T1.pop(key, None)
        _move_to_mru(_T2, key)
        _trim_ghosts()
        return
    if key in _T2:
        _move_to_mru(_T2, key)
        _trim_ghosts()
        return

    # Cap adaptation step to avoid overshoot
    step_cap = max(1, _g_capacity // 8)

    # Ghost hits drive adaptation of p
    if key in _B1:
        # Favor recency: raise p
        raw_inc = max(1, len(_B2) // max(1, len(_B1)))
        inc = min(step_cap, raw_inc)
        _p = min(float(_g_capacity), _p + inc)
        _B1.pop(key, None)
        _B2.pop(key, None)  # ensure disjointness
        _move_to_mru(_T2, key)
    elif key in _B2:
        # Favor frequency: lower p
        raw_dec = max(1, len(_B1) // max(1, len(_B2)))
        dec = min(step_cap, raw_dec)
        _p = max(0.0, _p - dec)
        _B2.pop(key, None)
        _B1.pop(key, None)  # ensure disjointness
        _move_to_mru(_T2, key)
    else:
        # First-time insertion: recency path
        _move_to_mru(_T1, key)

    # Keep ghosts bounded and metadata consistent
    if (len(_T1) + len(_T2)) > len(cache_snapshot.cache):
        _resync(cache_snapshot)
    _trim_ghosts()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After evicting a victim from the physical cache, move it into the appropriate ghost list.
    - If victim was in T1: move to B1.
    - If victim was in T2: move to B2.
    - If unknown (desync): prefer existing ghost membership (B2 first), else B1.
    Ghost lists remain disjoint and bounded.
    '''
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key

    had_t1 = k in _T1
    had_t2 = k in _T2
    _T1.pop(k, None)
    _T2.pop(k, None)

    if had_t2:
        _move_to_mru(_B2, k)
        _B1.pop(k, None)
    elif had_t1:
        _move_to_mru(_B1, k)
        _B2.pop(k, None)
    else:
        # Preserve any existing ghost preference; prefer B2 for frequency
        if k in _B2:
            _move_to_mru(_B2, k)
            _B1.pop(k, None)
        else:
            _move_to_mru(_B1, k)
            _B2.pop(k, None)

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