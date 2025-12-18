# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads
ARC+ with scan-awareness and ghost-hinted victim selection:
- Resident sets: T1 (recent), T2 (frequent).
- Ghost sets: B1 (recently evicted from T1), B2 (recently evicted from T2).
- Adaptive target p controls desired size of T1 with damping, decay and scan clamp.
- Ghost-aware intra-segment victim choice; scan-aware probation/demotion.
"""

# Resident metadata
# - m_key_timestamp: last access time for each resident key
# - m_key_segment: 't1' (recent) or 't2' (frequent) for resident keys
m_key_timestamp = dict()
m_key_segment = dict()

# Ghost metadata (key -> last timestamp when it entered ghost)
m_ghost_b1_ts = dict()
m_ghost_b2_ts = dict()

# Adaptive target for T1 size
m_target_p = None

# Additional controls
m_last_ghost_hit_access = None
m_last_ghost_hit_side = None  # 'B1' or 'B2'
m_cold_streak = 0  # count of consecutive cold misses (not in ghosts)
m_t1_pending_hits = dict()  # retained for compatibility, not used for promotion

# Lightweight frequency sketch with periodic decay to avoid long-term bias
m_freq = dict()  # key -> decaying frequency count (applies to recent activity)
m_next_decay_access = None  # next access_count at which to decay frequencies


def _cap(cache_snapshot):
    try:
        return int(cache_snapshot.capacity)
    except Exception:
        return max(1, len(cache_snapshot.cache))


def _ensure_init(cache_snapshot):
    global m_target_p, m_last_ghost_hit_access, m_last_ghost_hit_side, m_cold_streak, m_next_decay_access
    if m_target_p is None:
        m_target_p = max(1, _cap(cache_snapshot) // 2)
    if m_last_ghost_hit_access is None:
        m_last_ghost_hit_access = cache_snapshot.access_count
    if m_last_ghost_hit_side is None:
        m_last_ghost_hit_side = None
    if m_cold_streak is None:
        m_cold_streak = 0
    if m_next_decay_access is None:
        # Schedule frequency decay roughly once per capacity accesses
        m_next_decay_access = cache_snapshot.access_count + max(8, _cap(cache_snapshot))


def _resident_sets(cache_snapshot):
    """Return (t1_keys, t2_keys) among current cache keys, using metadata.
    Unknown residents are seeded ghost-aware to preserve ARC intent:
    - If key in B2 -> seed as T2; if in B1 -> seed as T1; else T1.
    Ghosts remain disjoint from residents.
    """
    cache_keys = set(cache_snapshot.cache.keys())
    t1_keys = []
    t2_keys = []
    for k in cache_keys:
        seg = m_key_segment.get(k, None)
        if seg is None:
            # Ghost-aware seeding
            if k in m_ghost_b2_ts:
                m_key_segment[k] = 't2'
                m_ghost_b2_ts.pop(k, None)
                m_ghost_b1_ts.pop(k, None)
                seg = 't2'
            elif k in m_ghost_b1_ts:
                m_key_segment[k] = 't1'
                m_ghost_b1_ts.pop(k, None)
                m_ghost_b2_ts.pop(k, None)
                seg = 't1'
            else:
                m_key_segment[k] = 't1'
                seg = 't1'
        if seg == 't2':
            t2_keys.append(k)
        else:
            if seg not in ('t1', 't2'):
                m_key_segment[k] = 't1'
            t1_keys.append(k)
    return t1_keys, t2_keys


def _lru_key(keys):
    """Return LRU key among `keys` using m_key_timestamp; None if empty."""
    if not keys:
        return None
    return min(keys, key=lambda k: m_key_timestamp.get(k, float('inf')))


def _prune_ghosts(cache_snapshot):
    """Keep total ghost size <= 2*capacity with a short-lived bias: after a ghost hit on one side,
    trim the opposite side first for up to C//8 accesses to retain the most recent signal.
    """
    total = len(m_ghost_b1_ts) + len(m_ghost_b2_ts)
    cap = _cap(cache_snapshot)
    limit = max(1, 2 * cap)
    now = cache_snapshot.access_count

    def _pop_oldest(d):
        if d:
            k = min(d, key=d.get)
            d.pop(k, None)

    bias_active = False
    if m_last_ghost_hit_access is not None and m_last_ghost_hit_side is not None:
        bias_window = max(1, cap // 8)
        bias_active = (now - m_last_ghost_hit_access) <= bias_window

    while total > limit:
        if bias_active:
            # Trim opposite to the last hit side first
            if m_last_ghost_hit_side == 'B1':
                if m_ghost_b2_ts:
                    _pop_oldest(m_ghost_b2_ts)
                elif m_ghost_b1_ts:
                    _pop_oldest(m_ghost_b1_ts)
            else:  # last side == 'B2'
                if m_ghost_b1_ts:
                    _pop_oldest(m_ghost_b1_ts)
                elif m_ghost_b2_ts:
                    _pop_oldest(m_ghost_b2_ts)
        else:
            # Default: trim from the larger ghost list to maintain balance
            if len(m_ghost_b1_ts) >= len(m_ghost_b2_ts):
                if m_ghost_b1_ts:
                    _pop_oldest(m_ghost_b1_ts)
                elif m_ghost_b2_ts:
                    _pop_oldest(m_ghost_b2_ts)
            else:
                if m_ghost_b2_ts:
                    _pop_oldest(m_ghost_b2_ts)
                elif m_ghost_b1_ts:
                    _pop_oldest(m_ghost_b1_ts)
        total = len(m_ghost_b1_ts) + len(m_ghost_b2_ts)


def _maybe_decay_freq(cache_snapshot):
    """Periodically decay frequency counts to bound memory and track recent popularity."""
    global m_freq, m_next_decay_access
    _ensure_init(cache_snapshot)
    if m_next_decay_access is None:
        m_next_decay_access = cache_snapshot.access_count + max(8, _cap(cache_snapshot))
        return
    if cache_snapshot.access_count >= m_next_decay_access:
        if m_freq:
            for k in list(m_freq.keys()):
                newc = m_freq.get(k, 0) >> 1  # halve counts
                if newc:
                    m_freq[k] = newc
                else:
                    m_freq.pop(k, None)
        m_next_decay_access = cache_snapshot.access_count + max(8, _cap(cache_snapshot))


def _bump_freq(key, weight=1):
    """Increase frequency count with small integer weight."""
    try:
        inc = max(1, int(weight))
    except Exception:
        inc = 1
    m_freq[key] = m_freq.get(key, 0) + inc


def _ghost_hint_rank(key):
    """Ranking for eviction preference using ghosts: prefer evicting B1, avoid B2."""
    if key in m_ghost_b1_ts:
        return 0  # best to evict
    if key in m_ghost_b2_ts:
        return 2  # worst to evict
    return 1  # neutral


def _choose_victim_with_hints(keys, now):
    """Pick victim using ghost hints, then lowest frequency, then oldest timestamp."""
    if not keys:
        return None
    return min(
        keys,
        key=lambda k: (_ghost_hint_rank(k), m_freq.get(k, 0), m_key_timestamp.get(k, float('inf')))
    )


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    _ensure_init(cache_snapshot)
    t1_keys, t2_keys = _resident_sets(cache_snapshot)

    # Note: Do NOT purge all ghost entries for residents here; that destroys history.
    # The disjointness is enforced on exact key events (hit/insert/evict).

    cap = _cap(cache_snapshot)
    p = min(max(0, m_target_p), cap)
    t1_len = len(t1_keys)
    now = cache_snapshot.access_count

    # ARC REPLACE decision with scan-aware, ghost-hinted selection
    evict_from_t1 = (t1_len > p) or (obj.key in m_ghost_b2_ts and t1_len >= max(1, p))

    if evict_from_t1:
        victim = _choose_victim_with_hints(t1_keys, now)
        if victim is not None:
            return victim
        # Fallback: T1 empty, try T2
        victim = _choose_victim_with_hints(t2_keys, now)
        if victim is not None:
            return victim
    else:
        victim = _choose_victim_with_hints(t2_keys, now)
        if victim is not None:
            return victim
        # Fallback: T2 empty, try T1
        victim = _choose_victim_with_hints(t1_keys, now)
        if victim is not None:
            return victim

    # Last resort: global ghost-hinted choice
    all_keys = list(cache_snapshot.cache.keys())
    if not all_keys:
        return None
    return _choose_victim_with_hints(all_keys, now)


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, m_key_segment, m_t1_pending_hits, m_cold_streak, m_target_p, m_last_ghost_hit_access, m_freq
    _ensure_init(cache_snapshot)

    # Periodic decay to keep frequency sketch recent
    _maybe_decay_freq(cache_snapshot)

    # Refresh recency
    now = cache_snapshot.access_count
    m_key_timestamp[obj.key] = now

    # Reset cold streak on any hit and bump frequency
    m_cold_streak = 0
    _bump_freq(obj.key, 1)

    # Idle decay of p since last ghost hit: faster recovery under scans
    cap = _cap(cache_snapshot)
    idle = max(0, now - m_last_ghost_hit_access)
    if idle > cap // 2:
        step_cap = max(1, cap // 8)
        dec = min(step_cap, idle // max(1, cap))
        if dec > 0:
            m_target_p = max(0, m_target_p - dec)

    # ARC-style immediate promotion: on a hit in T1, move to T2
    if m_key_segment.get(obj.key, 't1') != 't2':
        m_key_segment[obj.key] = 't2'
    # Clear any pending two-hit state (no longer used)
    m_t1_pending_hits.pop(obj.key, None)

    # Ensure ghosts remain disjoint from this resident
    m_ghost_b1_ts.pop(obj.key, None)
    m_ghost_b2_ts.pop(obj.key, None)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_key_segment, m_target_p, m_last_ghost_hit_access, m_last_ghost_hit_side, m_cold_streak, m_t1_pending_hits, m_freq
    _ensure_init(cache_snapshot)

    # Periodic decay to keep frequency sketch recent
    _maybe_decay_freq(cache_snapshot)

    cap = _cap(cache_snapshot)
    in_b1 = obj.key in m_ghost_b1_ts
    in_b2 = obj.key in m_ghost_b2_ts

    # Adaptive step caps with asymmetric responsiveness (scan-aware)
    inc_cap = max(1, cap // 8)
    dec_cap = max(1, (cap // 4) if m_cold_streak >= max(1, cap // 2) else (cap // 8))

    seg = 't1'
    now = cache_snapshot.access_count
    if in_b1:
        # Increase p (give more room to recency), with ceiling ratio and cap
        denom = max(1, len(m_ghost_b1_ts))
        numer = len(m_ghost_b2_ts)
        raw_inc = max(1, (numer + denom - 1) // denom)  # ceil(|B2|/|B1|)
        inc = min(inc_cap, raw_inc)
        m_target_p = min(cap, m_target_p + inc)
        # Ghost bookkeeping
        m_ghost_b1_ts.pop(obj.key, None)
        m_ghost_b2_ts.pop(obj.key, None)
        # Promote on ghost hit
        seg = 't2'
        m_last_ghost_hit_access = now
        m_last_ghost_hit_side = 'B1'
        m_cold_streak = 0
        _bump_freq(obj.key, 2)
    elif in_b2:
        # Decrease p (favor frequency), with ceiling ratio and stronger cap during cold streaks
        denom = max(1, len(m_ghost_b2_ts))
        numer = len(m_ghost_b1_ts)
        raw_dec = max(1, (numer + denom - 1) // denom)  # ceil(|B1|/|B2|)
        dec = min(dec_cap, raw_dec)
        m_target_p = max(0, m_target_p - dec)
        # Ghost bookkeeping
        m_ghost_b2_ts.pop(obj.key, None)
        m_ghost_b1_ts.pop(obj.key, None)
        # Promote on ghost hit
        seg = 't2'
        m_last_ghost_hit_access = now
        m_last_ghost_hit_side = 'B2'
        m_cold_streak = 0
        _bump_freq(obj.key, 3)
    else:
        # Cold miss - track streak and apply scan clamp to reduce T1 pressure
        m_cold_streak += 1
        # Gentle clamp every C//2
        if m_cold_streak % max(1, cap // 2) == 0:
            m_target_p = max(0, m_target_p - max(1, cap // 16))
        # Stronger clamp at >= C, then reset the counter
        if m_cold_streak >= cap:
            m_target_p = max(0, m_target_p - max(1, cap // 8))
            m_cold_streak = 0
        # Ensure no stale ghost entries remain for this resident
        m_ghost_b1_ts.pop(obj.key, None)
        m_ghost_b2_ts.pop(obj.key, None)
        _bump_freq(obj.key, 1)

    # Insert into resident set with scan-aware probation:
    # Under sustained cold streaks, insert at T1 LRU by stamping an older timestamp.
    m_key_segment[obj.key] = seg
    if not (in_b1 or in_b2) and m_cold_streak >= max(1, cap // 2):
        m_key_timestamp[obj.key] = now - (cap + 1)  # force to LRU position (probation)
    else:
        m_key_timestamp[obj.key] = now

    # Optional scan-aware demotion: move a few T2 LRUs to T1 during cold phases
    if m_cold_streak >= max(1, cap // 2):
        t1_keys, t2_keys = _resident_sets(cache_snapshot)
        k = min(2, max(1, cap // 16))
        if t2_keys:
            # Pick up to k oldest in T2 and demote to T1
            oldest_t2 = sorted(t2_keys, key=lambda x: m_key_timestamp.get(x, float('inf')))[:k]
            for dk in oldest_t2:
                m_key_segment[dk] = 't1'

    # Clear any stale pending state for this key
    m_t1_pending_hits.pop(obj.key, None)

    # Control ghost size (expanded history)
    _prune_ghosts(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_key_segment, m_ghost_b1_ts, m_ghost_b2_ts, m_t1_pending_hits
    _ensure_init(cache_snapshot)

    # Remove resident metadata for evicted key
    seg = m_key_segment.pop(evicted_obj.key, 't1')
    m_key_timestamp.pop(evicted_obj.key, None)
    m_t1_pending_hits.pop(evicted_obj.key, None)

    # Add to corresponding ghost list with current time to maintain LRU.
    # Ensure ghosts remain disjoint.
    ts = cache_snapshot.access_count
    if seg == 't2':
        m_ghost_b2_ts[evicted_obj.key] = ts
        m_ghost_b1_ts.pop(evicted_obj.key, None)
    else:
        m_ghost_b1_ts[evicted_obj.key] = ts
        m_ghost_b2_ts.pop(evicted_obj.key, None)

    # Control ghost size (expanded history)
    _prune_ghosts(cache_snapshot)

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