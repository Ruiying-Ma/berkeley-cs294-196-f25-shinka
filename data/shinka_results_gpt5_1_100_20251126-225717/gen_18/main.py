# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Adaptive Segmented LRU with ARC-like ghost feedback + lightweight decayed LFU.
# Live segments:
#  - Probation (recently inserted, not yet proven)
#  - Protected (proven frequent/reused)
# Ghost segments (no data, just metadata of evicted keys):
#  - m_ghost_probation (recent recency victims)
#  - m_ghost_protected (recent frequency victims)
# Target size of protected adapts based on ghost hits.
# Additionally, a decayed frequency (m_freq) informs scoring at eviction time.
# Enhancements:
#  - Window-based scan detection to require two touches before promotion during scans.
#  - Merit-biased admission guard: weakly admit after evicting a strong victim (TinyLFU-like).
#  - Proactive cooling of stale protected entries and adaptive decay interval.

m_ts = dict()                  # key -> last access timestamp
m_probation = set()            # probation segment membership
m_protected = set()            # protected segment membership
m_ghost_probation = dict()     # key -> timestamp (ghost of probation)
m_ghost_protected = dict()     # key -> timestamp (ghost of protected)
m_target_protected = None      # target number of protected entries
m_last_capacity = None         # remember capacity to re-init target if it changes
m_freq = dict()                # key -> decayed frequency score
m_last_decay_access = 0        # last access when decay applied
m_decay_interval = None        # accesses between global frequency decay
m_ghost_strength = dict()      # key -> last known frequency at eviction (for seeding)

# Admission guard state
m_last_victim_score = 0.0
m_last_victim_key = None

# Scan-mode and promotion tracking
m_scan_mode = False            # when True, require two touches in probation before promotion
m_probation_hits = dict()      # key -> hit count while in probation

# Windowed adaptation (rolling per ~5x capacity accesses)
m_win_start = 0
m_win_size = None
m_win_inserts = 0
m_win_hits = 0
m_win_promotions = 0
m_win_ghost_prob_hits = 0
m_win_ghost_prot_hits = 0
m_win_unique = 0
m_win_seen = set()             # per-window seen keys to estimate unique insert ratio


def _init_targets(cache_snapshot):
    global m_target_protected, m_last_capacity, m_decay_interval, m_win_size, m_win_start
    cap = cache_snapshot.capacity or max(len(cache_snapshot.cache), 1)
    if m_target_protected is None or m_last_capacity != cap:
        # Start balanced
        m_target_protected = max(1, int(cap * 0.5))
        m_last_capacity = cap
        # Recompute decay interval when capacity changes
        m_decay_interval = max(100, int(2.0 * cap))
        # Window spans ~5x capacity accesses for robust signal
        m_win_size = max(200, int(5.0 * cap))
        m_win_start = cache_snapshot.access_count
        _reset_window_counters()


def _oldest_key(candidates):
    # Return the key with the smallest timestamp among candidates
    return min(candidates, key=lambda k: m_ts.get(k, -1))


def _trim_ghosts(capacity):
    # Bound ghost lists to capacity (ARC heuristic). Also trim stored strengths.
    global m_ghost_probation, m_ghost_protected, m_ghost_strength
    def trim(ghost):
        if len(ghost) <= capacity:
            return
        over = len(ghost) - capacity
        for _ in range(over):
            kmin = min(ghost, key=lambda k: ghost[k])
            ghost.pop(kmin, None)
            m_ghost_strength.pop(kmin, None)
    trim(m_ghost_probation)
    trim(m_ghost_protected)


def _enforce_protected_quota():
    # Demote LRU from protected to probation until target is met
    global m_probation, m_protected
    while m_target_protected is not None and len(m_protected) > m_target_protected:
        demote_key = _oldest_key(m_protected)
        m_protected.discard(demote_key)
        m_probation.add(demote_key)


def _maybe_decay(cache_snapshot):
    # Apply exponential decay to frequency counters periodically to adapt to phase changes
    global m_last_decay_access, m_decay_interval, m_freq
    now = cache_snapshot.access_count
    if m_decay_interval is None:
        return
    if now - m_last_decay_access >= m_decay_interval:
        for k in list(m_freq.keys()):
            m_freq[k] *= 0.5
            if m_freq[k] < 1e-3:
                m_freq.pop(k, None)
        m_last_decay_access = now


def _priority(key, now, cap):
    # Higher is better to keep; eviction chooses minimum.
    # LRFU-like: freq - age / cap, where age = now - last access.
    age = now - m_ts.get(key, now)
    freq = m_freq.get(key, 0.0)
    lam = 1.0 / max(1, cap)
    return freq - lam * age


def _cool_protected(now, cap):
    # Proactively demote stale protected entries, limited work per call
    # Demote up to 2 coldest if they are clearly weak
    demotions = 0
    max_demotions = 2
    while m_protected and demotions < max_demotions:
        cand = min(m_protected, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
        if _priority(cand, now, cap) < 0.0 or len(m_protected) > (m_target_protected or 0):
            m_protected.discard(cand)
            m_probation.add(cand)
            demotions += 1
        else:
            break


def _reset_window_counters():
    global m_win_inserts, m_win_hits, m_win_promotions
    global m_win_ghost_prob_hits, m_win_ghost_prot_hits, m_win_unique, m_win_seen
    m_win_inserts = 0
    m_win_hits = 0
    m_win_promotions = 0
    m_win_ghost_prob_hits = 0
    m_win_ghost_prot_hits = 0
    m_win_unique = 0
    m_win_seen = set()


def _maybe_roll_window(cache_snapshot):
    global m_win_start, m_win_size, m_scan_mode, m_target_protected, m_decay_interval
    now = cache_snapshot.access_count
    if m_win_size is None or now - m_win_start < m_win_size:
        return
    cap = m_last_capacity or max(1, len(cache_snapshot.cache))

    # Window metrics
    total_inserts = max(1, m_win_inserts)
    total_accesses = max(1, m_win_hits + m_win_inserts)
    unique_insert_rate = m_win_unique / total_inserts
    recent_hit_rate = m_win_hits / total_accesses
    ghost_hits = m_win_ghost_prob_hits + m_win_ghost_prot_hits
    ghost_hit_rate = ghost_hits / total_inserts
    promotion_rate = m_win_promotions / total_inserts

    # Adjust protected target based on signals
    step_small = max(1, cap // 32)
    step_medium = max(1, cap // 20)

    # Grow protected when reuse signals are strong
    if ghost_hit_rate > 0.15 or promotion_rate > 0.25 or m_win_ghost_prot_hits > m_win_ghost_prob_hits:
        m_target_protected = min(int(0.7 * cap), m_target_protected + step_medium)

    # Shrink protected under scans (many uniques, poor hits)
    if unique_insert_rate > 0.6 and recent_hit_rate < 0.2:
        m_target_protected = max(int(0.2 * cap), m_target_protected - step_medium)

    # Determine scan mode: stricter promotion rule to resist scans
    m_scan_mode = (unique_insert_rate > 0.6 and recent_hit_rate < 0.2)

    # Adapt decay interval (shorter under scans, longer under reuse-heavy)
    target_half = int(0.8 * cap) if m_scan_mode else int(2.5 * cap)
    m_decay_interval = max(50, int(0.5 * m_decay_interval + 0.5 * target_half))

    # Reset window
    m_win_start = now
    _reset_window_counters()


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_ts, m_probation, m_protected, m_freq
    _init_targets(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    keys_in_cache = set(cache_snapshot.cache.keys())

    # Keep metadata consistent with actual cache content
    if m_probation:
        m_probation.intersection_update(keys_in_cache)
    if m_protected:
        m_protected.intersection_update(keys_in_cache)
    if m_ts:
        for k in list(m_ts.keys()):
            if k not in keys_in_cache:
                m_ts.pop(k, None)
                m_probation.discard(k)
                m_protected.discard(k)
                m_probation_hits.pop(k, None)
    if m_freq:
        for k in list(m_freq.keys()):
            if k not in keys_in_cache:
                m_freq.pop(k, None)

    probation_candidates = m_probation & keys_in_cache
    protected_candidates = m_protected & keys_in_cache

    # ARC-like choice of source segment based on target sizes
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)
    now = cache_snapshot.access_count

    # If protected is oversized, trim it; else prefer eviction from probation
    if protected_candidates and len(m_protected) > (m_target_protected or 0):
        return min(protected_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    if probation_candidates:
        return min(probation_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    if protected_candidates:
        return min(protected_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))

    # Fallback: evict the globally coldest if segmentation hasn't been set yet
    if keys_in_cache:
        return min(keys_in_cache, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_target_protected, m_freq, m_win_hits, m_win_promotions
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    now = cache_snapshot.access_count
    key = obj.key

    # Ensure timestamp exists and update recency
    m_ts[key] = now
    # Increase decayed frequency on hit
    m_freq[key] = m_freq.get(key, 0.0) + 1.0

    # Window: count hits
    m_win_hits += 1

    # Promote from probation on reuse; under scan mode require two touches
    if key in m_probation:
        m_probation_hits[key] = m_probation_hits.get(key, 0) + 1
        needed = 2 if m_scan_mode else 1
        if m_probation_hits[key] >= needed:
            m_probation.discard(key)
            m_probation_hits.pop(key, None)
            m_protected.add(key)
            # Slightly increase protected target on successful promotion (favor frequency)
            cap = m_last_capacity or max(len(cache_snapshot.cache), 1)
            delta = 1  # conservative step to avoid oscillation
            m_target_protected = min(cap, max(1, m_target_protected + delta))
            m_win_promotions += 1
    elif key not in m_protected:
        # If metadata was missing, treat as protected to avoid premature eviction
        m_protected.add(key)

    # Enforce protected quota by demoting its LRU if needed and cool stale protected
    _enforce_protected_quota()
    _cool_protected(now, m_last_capacity or 1)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected
    global m_target_protected, m_freq, m_ghost_strength
    global m_win_inserts, m_win_unique, m_win_ghost_prob_hits, m_win_ghost_prot_hits, m_win_seen
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    now = cache_snapshot.access_count
    key = obj.key
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # Window: record miss/insert and uniqueness
    m_win_inserts += 1
    if key not in m_win_seen:
        m_win_seen.add(key)
        m_win_unique += 1

    # Capture ghost presence/strength before we mutate
    was_ghost_prob = key in m_ghost_probation
    was_ghost_prot = key in m_ghost_protected
    prev_strength = m_ghost_strength.pop(key, 0.0)

    # ARC-like adaptation based on ghost hits:
    step = max(1, cap // 32)
    if was_ghost_prob:
        m_target_protected = max(1, m_target_protected - step)
        m_ghost_probation.pop(key, None)
        m_win_ghost_prob_hits += 1
    elif was_ghost_prot:
        m_target_protected = min(cap, m_target_protected + step)
        m_ghost_protected.pop(key, None)
        m_win_ghost_prot_hits += 1

    # Insert starts in probation
    m_ts[key] = now
    m_protected.discard(key)
    m_probation.add(key)
    m_probation_hits[key] = 0

    # Seed a small initial frequency; boost if recent ghost or remembered strength
    # Admission guard: if previous victim was strong and no ghost, seed weak to prefer quick eviction.
    base = 0.1
    strong_victim = (m_last_victim_score > 2.0)
    if was_ghost_prot:
        base = max(base, 1.5)
    elif was_ghost_prob:
        base = max(base, 0.6)
    elif strong_victim:
        base = 0.0
    if prev_strength > 0:
        base = max(base, prev_strength * 0.7)
    m_freq[key] = base

    # Respect current target by demoting protected LRU if over target and cool stale ones
    _enforce_protected_quota()
    _cool_protected(now, cap)

    # Keep ghost lists bounded
    _trim_ghosts(cap)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected, m_freq, m_ghost_strength
    global m_last_victim_score, m_last_victim_key
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    evk = evicted_obj.key
    now = cache_snapshot.access_count
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # Determine segment before removal
    was_protected = evk in m_protected

    # Capture and remove all metadata for the evicted object
    last_strength = m_freq.pop(evk, 0.0)
    # Compute victim priority score before removing timestamps
    score = _priority(evk, now, cap) if evk in m_ts else last_strength
    m_ts.pop(evk, None)
    m_probation.discard(evk)
    m_protected.discard(evk)
    m_probation_hits.pop(evk, None)

    # Record into appropriate ghost list (ARC feedback) and remember strength
    if was_protected:
        m_ghost_protected[evk] = now
    else:
        # If unknown or probation, treat as probation ghost
        m_ghost_probation[evk] = now
    if last_strength > 0:
        m_ghost_strength[evk] = last_strength

    # Admission guard signal for next insert
    m_last_victim_score = score
    m_last_victim_key = evk

    # Trim ghosts to capacity
    _trim_ghosts(cap)

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