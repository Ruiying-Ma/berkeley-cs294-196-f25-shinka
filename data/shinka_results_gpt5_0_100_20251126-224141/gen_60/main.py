# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# Global timestamps for fallback/tie-breaks
m_key_timestamp = dict()

# ARC state
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None     # will be initialized from cache_snapshot

# Adaptation control
last_ghost_hit_access = 0   # last time we saw a ghost hit
cold_streak = 0             # consecutive evictions/admissions without ghost/hit signal
scan_guard_until = 0        # temporarily bias REPLACE toward T1 during scans


def _ensure_capacity(cache_snapshot):
    global arc_capacity, arc_p
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)
    # Keep p within [0, C]
    arc_p = min(max(arc_p, 0), arc_capacity)


def _move_to_mru(od, key):
    if key in od:
        od.pop(key, None)
    od[key] = True


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _trim_ghosts():
    # Keep ghosts total length within 2*C, and bias trimming according to p split
    cap = arc_capacity if arc_capacity is not None else 1
    global arc_p
    arc_p = min(max(arc_p, 0), cap)
    total_cap = 2 * cap
    while (len(arc_B1) + len(arc_B2)) > total_cap:
        target_B1 = min(cap, arc_p)
        target_B2 = max(0, cap - target_B1)
        excess_B1 = max(0, len(arc_B1) - target_B1)
        excess_B2 = max(0, len(arc_B2) - target_B2)
        if excess_B1 >= excess_B2 and arc_B1:
            _pop_lru(arc_B1)
        elif arc_B2:
            _pop_lru(arc_B2)
        else:
            # If both within targets but overall still exceeds (rounding), trim larger
            if len(arc_B1) >= len(arc_B2) and arc_B1:
                _pop_lru(arc_B1)
            elif arc_B2:
                _pop_lru(arc_B2)
            else:
                break


def _resync(cache_snapshot):
    # Ensure residents track actual cache and ghosts are disjoint
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Any resident keys not tracked go to T1 (recent)
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            _move_to_mru(arc_T1, k)
    # Ghosts must not contain residents
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _decay_p_if_idle(now):
    # Decay p slowly when there has been no ghost signal for a while
    global arc_p
    if arc_capacity is None:
        return
    idle = now - last_ghost_hit_access
    if idle <= 0:
        return
    # Base decay proportional to idle, bounded
    base_step = max(1, idle // max(1, arc_capacity // 4))
    arc_p = max(0, arc_p - min(base_step, max(1, arc_capacity // 8)))


def _adapt_p_on_ghost(now, obj_key):
    # Adjust p when we see a ghost hit, clamp, and reset cold streak/guard
    global arc_p, last_ghost_hit_access, cold_streak, scan_guard_until
    if arc_capacity is None:
        return
    cap = arc_capacity
    if obj_key in arc_B1:
        # Favor recency: grow T1 target
        step = max(1, len(arc_B2) // max(1, len(arc_B1)))
        arc_p = min(cap, arc_p + min(step, max(1, cap // 8)))
        last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = 0
    elif obj_key in arc_B2:
        # Favor frequency: shrink T1 target, more aggressively if prolonged cold streak
        step = max(1, len(arc_B1) // max(1, len(arc_B2)))
        maxstep = max(1, (cap // 4) if cold_streak >= max(1, cap // 2) else (cap // 8))
        arc_p = max(0, arc_p - min(step, maxstep))
        last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = 0


def _maybe_enable_scan_guard(now, is_ghost):
    # On sustained cold streaks, bias REPLACE toward evicting from T1
    global scan_guard_until
    if arc_capacity is None:
        return
    cap = arc_capacity
    if is_ghost:
        return
    if cold_streak >= max(1, cap // 2):
        # Guard window for a short period
        scan_guard_until = max(scan_guard_until, now + max(1, cap // 8))


def _replace_choose_from(which):
    # Peek LRU key from a chosen list without mutating
    if which == 'T1':
        return next(iter(arc_T1)) if arc_T1 else None
    else:
        return next(iter(arc_T2)) if arc_T2 else None


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

    now = cache_snapshot.access_count
    # Adjust p if this is a ghost hit, and track cold/guard
    is_ghost = (obj.key in arc_B1) or (obj.key in arc_B2)
    _adapt_p_on_ghost(now, obj.key)
    if not is_ghost:
        # cold admission; increment cold streak and maybe enable guard
        # This runs per miss leading to eviction
        # Note: hits reset cold_streak elsewhere
        globals()['cold_streak'] = globals()['cold_streak'] + 1
        _maybe_enable_scan_guard(now, is_ghost)
    else:
        # Ensure cold streak stays reset on ghost signal
        globals()['cold_streak'] = 0

    # Idle decay of p to recover faster after long idle/no-ghost periods
    _decay_p_if_idle(now)

    # Canonical ARC REPLACE decision with optional scan guard bias
    t1_sz = len(arc_T1)
    choose_T1 = (t1_sz >= 1 and (t1_sz > arc_p or (obj.key in arc_B2 and t1_sz == arc_p)))

    # If scan guard active, bias toward T1 unless T1 empty
    if scan_guard_until > now:
        if arc_T1:
            choose_T1 = True
        elif arc_T2:
            choose_T1 = False

    candidate = None
    if choose_T1:
        candidate = _replace_choose_from('T1')
        if candidate is None:
            candidate = _replace_choose_from('T2')
    else:
        candidate = _replace_choose_from('T2')
        if candidate is None:
            candidate = _replace_choose_from('T1')

    if candidate is None:
        # Fallback: oldest by timestamp; else any key
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
    Update metadata after a cache hit.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    # ARC hit handling: promote/refresh to T2
    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    elif key in arc_T2:
        _move_to_mru(arc_T2, key)
    else:
        # Untracked but hit: place into T2 to protect
        _move_to_mru(arc_T2, key)

    # Ghosts must be disjoint with residents
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    # Timestamps used for fallback victim selection
    m_key_timestamp[key] = now

    # Any hit indicates locality; reset cold streak and guard
    global cold_streak, scan_guard_until
    cold_streak = 0
    scan_guard_until = 0


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after inserting a new object into the cache.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    # ARC admission: ghost re-references go directly to T2; brand new to T1
    if key in arc_B1:
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
        # Ghost hit confirmation
        global last_ghost_hit_access, cold_streak, scan_guard_until
        last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = 0
    elif key in arc_B2:
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
        global last_ghost_hit_access, cold_streak, scan_guard_until
        last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = 0
    else:
        _move_to_mru(arc_T1, key)

    # Keep ghosts tidy and timestamps fresh
    _trim_ghosts()
    m_key_timestamp[key] = now


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after evicting the victim.
    '''
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key

    # Move evicted resident to corresponding ghost list
    if k in arc_T1:
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
    else:
        # Unknown membership: default to B1
        _move_to_mru(arc_B1, k)

    # Shrink ghosts if needed and drop timestamp
    _trim_ghosts()
    m_key_timestamp.pop(k, None)

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