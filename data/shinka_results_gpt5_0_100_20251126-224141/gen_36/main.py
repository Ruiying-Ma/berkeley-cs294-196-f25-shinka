# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LRU timestamp map used as a tie-breaker and fallback
m_key_timestamp = dict()

# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None     # will be initialized from cache_snapshot

# Delayed promotion and adaptation state
t1_pending = dict()             # key -> last hit access_count while in T1
arc_last_ghost_hit_access = 0   # last access_count when a ghost hit occurred
arc_last_decay_access = 0       # throttle decay operations
cold_streak = 0                 # consecutive cold admissions without ghost/hit signal

def _pending_window():
    cap = arc_capacity if arc_capacity is not None else 1
    return max(1, cap // 4)


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
    # Keep ghosts total size within 2x capacity with proportional trimming to p
    cap_base = (arc_capacity if arc_capacity is not None else 1)
    total_cap = cap_base * 2
    # Targets proportional to current p (approximately 2*p and 2C-2*p)
    target_B1 = min(total_cap, max(0, int(total_cap * (float(arc_p) / max(1, cap_base)))))
    target_B2 = max(0, total_cap - target_B1)

    def _over_target():
        return (len(arc_B1) + len(arc_B2)) - total_cap

    # Trim until under the total budget, favoring lists above their targets
    while (len(arc_B1) + len(arc_B2)) > total_cap:
        if len(arc_B1) > target_B1:
            _pop_lru(arc_B1)
        elif len(arc_B2) > target_B2:
            _pop_lru(arc_B2)
        else:
            # If neither is over its proportional target, drop from the larger one
            if len(arc_B1) >= len(arc_B2):
                _pop_lru(arc_B1)
            else:
                _pop_lru(arc_B2)

# Decay controller: if no ghost hits for a while, slowly bias toward recency (smaller p)
def _decay_arc_p_if_idle(now):
    global arc_p, arc_last_decay_access
    if arc_capacity is None:
        return
    # Only decay if we've had no ghost hits for at least one cache of accesses
    idle = now - arc_last_ghost_hit_access
    step_interval = max(1, arc_capacity // 8)
    if idle >= arc_capacity and (now - arc_last_decay_access) >= step_interval:
        arc_p = max(0, arc_p - 1)
        arc_last_decay_access = now


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
    # ARC replacement: choose between T1 and T2 depending on p and whether obj is in B2
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    victim = None
    if t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)):
        # Evict LRU from T1
        victim = next(iter(arc_T1)) if arc_T1 else None
    else:
        # Evict LRU from T2
        victim = next(iter(arc_T2)) if arc_T2 else None

    # Strict ARC fallback: use the other list if the chosen one is empty
    if victim is None:
        if t1_sz > 0:
            victim = next(iter(arc_T1))
        elif len(arc_T2) > 0:
            victim = next(iter(arc_T2))
        else:
            # Rare drift: resync once and retry, then age-based deterministic fallback
            _resync(cache_snapshot)
            if arc_T1:
                victim = next(iter(arc_T1))
            elif arc_T2:
                victim = next(iter(arc_T2))
            elif cache_snapshot.cache:
                # Choose the oldest timestamped key to approximate LRU
                if m_key_timestamp:
                    victim = min(cache_snapshot.cache.keys(), key=lambda k: m_key_timestamp.get(k, 0))
                else:
                    victim = next(iter(cache_snapshot.cache.keys()))
    return victim


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
    now = cache_snapshot.access_count
    _decay_arc_p_if_idle(now)
    # Any hit breaks cold streaks
    cold_streak = 0

    # Keep resident metadata consistent with actual cache
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    key = obj.key
    if key in arc_T1:
        # Canonical ARC: on a hit in T1, move to T2 (become frequent)
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
        t1_pending.pop(key, None)
    elif key in arc_T2:
        # Refresh recency within T2
        _move_to_mru(arc_T2, key)
    else:
        # Metadata drift: conservatively place into T1 as recent
        _move_to_mru(arc_T1, key)

    # Maintain disjointness: resident keys must not appear in ghosts
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    _trim_ghosts()
    # Update timestamp for tie-breaking/fallback
    m_key_timestamp[key] = now


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, arc_p, arc_last_ghost_hit_access, cold_streak
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    _decay_arc_p_if_idle(now)
    key = obj.key

    # Keep resident metadata consistent with actual cache
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    step_cap = max(1, (arc_capacity if arc_capacity is not None else 1) // 8)

    # ARC admission policy with bounded, responsive p-updates
    if key in arc_B1:
        # Previously evicted from T1: favor recency by increasing p
        ratio = max(1, len(arc_B2) // max(1, len(arc_B1)))
        inc = min(ratio, step_cap, max(0, arc_capacity - arc_p))
        arc_p = min(arc_capacity, arc_p + inc)
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
        arc_last_ghost_hit_access = now
        cold_streak = 0
    elif key in arc_B2:
        # Previously frequent: favor frequency by decreasing p
        ratio = max(1, len(arc_B1) // max(1, len(arc_B2)))
        dec = min(ratio, step_cap, max(0, arc_p))
        arc_p = max(0, arc_p - dec)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
        arc_last_ghost_hit_access = now
        cold_streak = 0
    else:
        # Brand new: insert into T1 (recent)
        _move_to_mru(arc_T1, key)
        cold_streak += 1
        # Gentler scan clamp: after a long cold streak, bias slightly toward recency
        if arc_capacity is not None and cold_streak > (2 * arc_capacity):
            arc_p = max(0, arc_p - max(1, arc_capacity // 16))
            t1_pending.clear()
            cold_streak = 0

    # Maintain disjointness: resident keys must not appear in ghosts
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    _trim_ghosts()
    m_key_timestamp[key] = now


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
    # Move evicted resident to corresponding ghost list, keeping ghosts disjoint
    if k in arc_T1:
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
        arc_B2.pop(k, None)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
        arc_B1.pop(k, None)
    else:
        # Unknown membership: prefer B2 if it already exists there, otherwise B1
        if k in arc_B2:
            _move_to_mru(arc_B2, k)
            arc_B1.pop(k, None)
        else:
            _move_to_mru(arc_B1, k)
            arc_B2.pop(k, None)
    # Clean up metadata for evicted item
    m_key_timestamp.pop(k, None)
    t1_pending.pop(k, None)
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