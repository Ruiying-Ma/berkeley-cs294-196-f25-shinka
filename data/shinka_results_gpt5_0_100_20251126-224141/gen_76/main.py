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

# Adaptation and phase tracking
arc_last_ghost_hit_access = 0   # last access_count when a ghost hit occurred
arc_last_decay_access = 0       # throttle decay operations
cold_streak = 0                 # consecutive cold admissions without ghost/hit signal
scan_guard_until = 0            # scan guard window end (access_count)
extra_cold_clamp_applied = False  # one-time clamp flag within a cold phase
t1_pending = dict()  # leftover from earlier designs (kept for safety)


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
    """
    Keep ghosts total size within capacity and p-aware:
    |B1| + |B2| <= arc_capacity,
    favor trimming the side exceeding its per-side target:
      target_B1 = arc_p, target_B2 = capacity - arc_p
    """
    cap = (arc_capacity if arc_capacity is not None else 1)
    # Clamp p for safety
    p = arc_p
    if p < 0:
        p = 0
    elif p > cap:
        p = cap
    target_B1 = p
    target_B2 = max(0, cap - p)

    while (len(arc_B1) + len(arc_B2)) > cap:
        if len(arc_B1) > target_B1:
            _pop_lru(arc_B1)
        elif len(arc_B2) > target_B2:
            _pop_lru(arc_B2)
        else:
            # Otherwise trim from the larger list
            if len(arc_B1) >= len(arc_B2):
                _pop_lru(arc_B1)
            else:
                _pop_lru(arc_B2)


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
    # Maintain disjointness: residents must not be in ghosts
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


# Proportional, bounded idle decay with one-time extra clamp for sustained cold phases
def _decay_arc_p_if_idle(now):
    global arc_p, arc_last_decay_access, extra_cold_clamp_applied
    if arc_capacity is None:
        return
    cap = arc_capacity
    idle = now - arc_last_ghost_hit_access
    # Throttle decay checks
    if (now - arc_last_decay_access) < max(1, cap // 16):
        return
    if idle > 0 and arc_p > 0:
        # Proportional bounded decay: recover faster the longer we go without ghost hits
        step = max(1, idle // max(1, cap // 4))
        arc_p = max(0, arc_p - min(max(1, cap // 8), step))
        arc_last_decay_access = now
    # One-time extra clamp during a cold streak; reset on ghost hit (in evict/insert)
    if (not extra_cold_clamp_applied) and (cold_streak >= max(1, cap // 2)) and arc_p > 0:
        extra = min(max(1, cap // 4), max(1, cold_streak // max(1, cap // 8)))
        arc_p = max(0, arc_p - extra)
        extra_cold_clamp_applied = True
        arc_last_decay_access = now


def _peek_lru(od):
    # Safe, non-mutating LRU peek on OrderedDict
    for k in od.keys():
        return k
    return None


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global arc_p, arc_last_ghost_hit_access, cold_streak, scan_guard_until, extra_cold_clamp_applied
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)

    cap = arc_capacity if arc_capacity is not None else 1
    now = cache_snapshot.access_count
    _decay_arc_p_if_idle(now)

    # Consolidated ghost-driven adaptation (pre-REPLACE)
    if obj.key in arc_B1:
        # Recency pressure ⇒ enlarge T1 target
        denom = max(1, len(arc_B1))
        numer = len(arc_B2)
        raw_inc = max(1, (numer + denom - 1) // denom)  # ceil(|B2|/|B1|)
        arc_p = min(cap, arc_p + min(max(1, cap // 8), raw_inc))
        arc_last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = now
        extra_cold_clamp_applied = False
    elif obj.key in arc_B2:
        # Frequency pressure ⇒ shrink T1 target (stronger during long cold streaks)
        denom = max(1, len(arc_B2))
        numer = len(arc_B1)
        raw_dec = max(1, (numer + denom - 1) // denom)  # ceil(|B1|/|B2|)
        cap_dec = max(1, (cap // 4) if cold_streak >= max(1, cap // 2) else (cap // 8))
        dec = min(raw_dec, cap_dec, max(0, arc_p))
        arc_p = max(0, arc_p - dec)
        arc_last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = now
        extra_cold_clamp_applied = False
    # Clamp p
    if arc_p < 0:
        arc_p = 0
    elif arc_p > cap:
        arc_p = cap

    # Scan guard bias during active window
    effective_p = arc_p
    if now < scan_guard_until:
        effective_p = max(0, arc_p - max(1, cap // 8))

    # Canonical ARC REPLACE decision
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    victim = None
    if t1_sz >= 1 and (t1_sz > effective_p or (x_in_B2 and t1_sz == effective_p)):
        victim = _peek_lru(arc_T1)
    else:
        victim = _peek_lru(arc_T2)

    # Strengthened deterministic fallback if chosen list is empty
    if victim is None:
        # (a) T1 LRU not in B2
        if arc_T1:
            k = _peek_lru(arc_T1)
            if k is not None and k not in arc_B2:
                victim = k

        # (b) T2 LRU that appears in B1
        if victim is None and arc_T2:
            k = _peek_lru(arc_T2)
            if k is not None and k in arc_B1:
                victim = k

        # (c) Depth-limited probing from T1 then T2 for a key not in B2
        if victim is None:
            d = min(8, max(1, cap // 16))
            # Probe T1 from LRU→MRU
            t1_keys = list(arc_T1.keys())
            for i in range(min(d, len(t1_keys))):
                if t1_keys[i] not in arc_B2:
                    victim = t1_keys[i]
                    break
        if victim is None:
            d = min(8, max(1, cap // 16))
            t2_keys = list(arc_T2.keys())
            for i in range(min(d, len(t2_keys))):
                candidate = t2_keys[i]
                # Prefer ones with a hint from B1
                if candidate in arc_B1 or candidate not in arc_B2:
                    victim = candidate
                    break

        # (d) Timestamp tie-breaker restricted to T1 keys, else arbitrary
        if victim is None and arc_T1:
            victim = min(arc_T1.keys(), key=lambda k: m_key_timestamp.get(k, 0))
        if victim is None and arc_T2:
            victim = min(arc_T2.keys(), key=lambda k: m_key_timestamp.get(k, 0))
        if victim is None and cache_snapshot.cache:
            # As a last resort, pick the oldest cached key by timestamp
            if m_key_timestamp:
                victim = min(cache_snapshot.cache.keys(), key=lambda k: m_key_timestamp.get(k, 0))
            else:
                for k in cache_snapshot.cache.keys():
                    victim = k
                    break

    return victim


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
    now = cache_snapshot.access_count
    _decay_arc_p_if_idle(now)
    # Any hit breaks cold streaks and cancels scan guard
    cold_streak = 0
    scan_guard_until = now

    # Keep resident metadata consistent with actual cache
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    key = obj.key
    if key in arc_T1:
        # On hit in T1, move to T2 (become frequent)
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
    global m_key_timestamp, arc_last_ghost_hit_access, cold_streak, scan_guard_until, extra_cold_clamp_applied
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    _decay_arc_p_if_idle(now)
    key = obj.key

    # Keep resident metadata consistent with actual cache
    if (len(arc_T1) + len(arc_T2)) != len(cache_snapshot.cache):
        _resync(cache_snapshot)

    cap = arc_capacity if arc_capacity is not None else 1

    # ARC admission without p-update (p already adjusted in evict on ghost hit)
    if key in arc_B1 or key in arc_B2:
        # Promote on ghost hit to T2
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
        # Mark recent ghost activity; cold streak broken and cancel scan guard
        arc_last_ghost_hit_access = now
        cold_streak = 0
        scan_guard_until = now
        extra_cold_clamp_applied = False
    else:
        # Brand new: insert into T1 (recent) and extend cold streak
        _move_to_mru(arc_T1, key)
        cold_streak += 1
        # Enable a short scan guard window during sustained cold streaks
        if cold_streak >= max(1, cap // 2):
            scan_guard_until = now + max(1, cap // 8)
        # Keep ghosts disjoint
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
    # Clean up timestamp for evicted item
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