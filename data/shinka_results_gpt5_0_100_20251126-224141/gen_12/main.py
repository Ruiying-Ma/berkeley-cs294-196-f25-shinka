# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Hybrid ARC-inspired Segmented LRU with stability improvements:
# - m_key_timestamp: last access time (for LRU ordering within segments)
# - m_tier: 'A1' (probation; seen once) or 'Am' (protected; seen >=2 times)
# - m_freq: simple hit count (tie-breaker and light frequency signal)
# - ghost_a1_ts / ghost_am_ts: ghost history of evicted keys with last-seen time
# - m_a1_target_count: adaptive target size (in items) for A1 (ARC-like "p")
# - m_pending_promote: delayed promotion marker for A1 items (promote to Am on 2nd hit within a window)
# - m_last_ghost_hit_access: last access index when a ghost-guided re-entry occurred (for damping)
# - m_cold_streak: count of consecutive inserts not seen in ghosts (scan clamp)
m_key_timestamp = dict()
m_tier = dict()
m_freq = dict()
ghost_a1_ts = dict()
ghost_am_ts = dict()
m_a1_target_count = None  # adaptive target for number of items in A1
m_pending_promote = dict()
m_last_ghost_hit_access = -1
m_cold_streak = 0


def _cap(snapshot):
    """Effective capacity proxy for time constants."""
    try:
        cap = int(getattr(snapshot, 'capacity', 0) or 0)
    except Exception:
        cap = 0
    if cap <= 0:
        cap = len(getattr(snapshot, 'cache', {}) or {})
    return max(cap, 1)


def _trim_ghosts(current_cache_size: int):
    """Keep ghost lists bounded; remove oldest entries first."""
    # Bound to at most 2x current cache size (but at least 100 to keep some history)
    bound = max(2 * max(current_cache_size, 1), 100)

    def trim(d):
        if len(d) <= bound:
            return
        # Remove oldest until under bound
        excess = len(d) - bound
        for k, _ in sorted(d.items(), key=lambda kv: kv[1])[:excess]:
            d.pop(k, None)

    trim(ghost_a1_ts)
    trim(ghost_am_ts)


def _current_a1_am_keys(cache_snapshot):
    keys = list(cache_snapshot.cache.keys())
    a1 = [k for k in keys if m_tier.get(k, 'A1') == 'A1']
    am = [k for k in keys if m_tier.get(k) == 'Am']
    # Any not in m_tier default to A1
    missing = [k for k in keys if k not in m_tier]
    if missing:
        a1.extend(missing)
    return a1, am, keys


def _decay_and_scan_controls(cache_snapshot):
    """Damped decay of A1 target and scan clamp based on lack of ghost signals and cold streaks."""
    global m_a1_target_count, m_last_ghost_hit_access, m_cold_streak
    total = len(getattr(cache_snapshot, 'cache', {}))
    if m_a1_target_count is None:
        m_a1_target_count = max(1, total // 3)
    cap = _cap(cache_snapshot)

    # Decay target when no ghost-driven signals for about one capacity worth of accesses.
    if m_last_ghost_hit_access >= 0 and (cache_snapshot.access_count - m_last_ghost_hit_access) > cap:
        m_a1_target_count = max(1, m_a1_target_count - 1)
        # pace the decay to at most 1 per 'cap' accesses
        m_last_ghost_hit_access = cache_snapshot.access_count

    # Scan clamp when many cold inserts in a row with no ghost evidence
    if m_cold_streak > cap and total > 0:
        m_a1_target_count = max(1, m_a1_target_count - max(1, total // 4))
        # dampen repeated clamping
        m_cold_streak = cap // 2


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_key_timestamp, m_tier, m_freq, m_a1_target_count, m_cold_streak
    a1_keys, am_keys, all_keys = _current_a1_am_keys(cache_snapshot)
    if not all_keys:
        return None

    total = len(all_keys)
    # Initialize target if not set; start with ~1/3 in A1
    if m_a1_target_count is None:
        m_a1_target_count = max(1, total // 3)

    # Effective target for this decision: adjust on-the-fly based on ghost hint for incoming obj
    step = max(1, total // 8)  # bounded, smoother adaptation
    effective_target = m_a1_target_count
    if obj is not None:
        if obj.key in ghost_a1_ts:
            effective_target = min(max(1, effective_target + step), max(total - 1, 1))
        elif obj.key in ghost_am_ts:
            effective_target = max(1, min(effective_target - step, max(total - 1, 1)))

    # Scan clamp: bias eviction toward A1 during long cold streaks
    if m_cold_streak > _cap(cache_snapshot):
        effective_target = max(1, min(effective_target - max(1, total // 4), max(total - 1, 1)))

    # Clamp target to sensible range
    effective_target = max(1, min(effective_target, max(total - 1, 1)))

    # ARC-like replacement: if A1 is larger than target, evict from A1, else from Am
    if a1_keys and (len(a1_keys) > effective_target or not am_keys):
        pick_from = a1_keys
    elif am_keys:
        pick_from = am_keys
    else:
        pick_from = all_keys

    # Choose LRU within chosen segment; tie-break by lowest frequency
    def ts(k): return m_key_timestamp.get(k, -1)
    min_ts = min(ts(k) for k in pick_from)
    ts_candidates = [k for k in pick_from if ts(k) == min_ts]
    if len(ts_candidates) > 1:
        min_f = min(m_freq.get(k, 1) for k in ts_candidates)
        freq_candidates = [k for k in ts_candidates if m_freq.get(k, 1) == min_f]
        candid_obj_key = freq_candidates[0]
    else:
        candid_obj_key = ts_candidates[0]
    return candid_obj_key


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq, m_pending_promote, m_cold_streak
    # Update recency and frequency
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_freq[obj.key] = m_freq.get(obj.key, 0) + 1

    # Delayed promotion: require two hits within a short window to move from A1 to Am
    tier = m_tier.get(obj.key, 'A1')
    if tier == 'A1':
        W = max(2, _cap(cache_snapshot) // 4)
        t_prev = m_pending_promote.get(obj.key)
        if t_prev is None:
            m_pending_promote[obj.key] = cache_snapshot.access_count
        else:
            if cache_snapshot.access_count - t_prev <= W:
                m_tier[obj.key] = 'Am'
                m_pending_promote.pop(obj.key, None)
                if m_freq.get(obj.key, 0) < 2:
                    m_freq[obj.key] = 2
            else:
                # Window expired; restart pending timer
                m_pending_promote[obj.key] = cache_snapshot.access_count
    else:
        # Already protected: ensure no pending state remains
        m_pending_promote.pop(obj.key, None)

    # Hits break cold streak
    m_cold_streak = 0

    # Remove from ghosts if it reappears
    ghost_a1_ts.pop(obj.key, None)
    ghost_am_ts.pop(obj.key, None)

    # Damped target decay and scan clamp controls
    _decay_and_scan_controls(cache_snapshot)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq, m_a1_target_count, m_last_ghost_hit_access, m_cold_streak, m_pending_promote
    # Set recency
    m_key_timestamp[obj.key] = cache_snapshot.access_count

    # If this key exists in ghosts, adapt target and insert into protected
    in_b1 = obj.key in ghost_a1_ts
    in_b2 = obj.key in ghost_am_ts

    total = len(getattr(cache_snapshot, 'cache', {}))
    if m_a1_target_count is None:
        m_a1_target_count = max(1, total // 3)

    if in_b1 or in_b2:
        # ARC adaptation of target p with bounded step
        step = max(1, total // 8)
        if in_b1:
            m_a1_target_count = min(m_a1_target_count + step, max(total - 1, 1))
        if in_b2:
            m_a1_target_count = max(1, min(m_a1_target_count - step, max(total - 1, 1)))

        # Insert into protected as it has demonstrated reuse
        m_tier[obj.key] = 'Am'
        m_freq[obj.key] = max(m_freq.get(obj.key, 0) + 1, 2)  # ensure >=2
        # Clear from ghosts since it's now resident
        ghost_a1_ts.pop(obj.key, None)
        ghost_am_ts.pop(obj.key, None)
        # Reset cold streak and mark last ghost influence
        m_cold_streak = 0
        m_last_ghost_hit_access = cache_snapshot.access_count
    else:
        # Fresh object: start in probation
        m_tier[obj.key] = 'A1'
        m_freq[obj.key] = 1
        # Count toward cold streak (no ghost signal)
        m_cold_streak += 1

    # No pending promotion on insert
    m_pending_promote.pop(obj.key, None)

    # Trim ghost history to bounded size and adjust controls
    _trim_ghosts(len(cache_snapshot.cache))
    _decay_and_scan_controls(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq, m_pending_promote
    # Record ghost of evicted key based on its segment at eviction time
    evicted_key = evicted_obj.key
    evicted_tier = m_tier.get(evicted_key, 'A1')

    # Insert into ghost lists with current time
    if evicted_tier == 'Am':
        ghost_am_ts[evicted_key] = cache_snapshot.access_count
    else:
        ghost_a1_ts[evicted_key] = cache_snapshot.access_count

    # Clean up all metadata for the evicted key
    m_key_timestamp.pop(evicted_key, None)
    m_tier.pop(evicted_key, None)
    m_freq.pop(evicted_key, None)
    m_pending_promote.pop(evicted_key, None)

    # Trim ghosts regularly
    _trim_ghosts(len(cache_snapshot.cache))

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