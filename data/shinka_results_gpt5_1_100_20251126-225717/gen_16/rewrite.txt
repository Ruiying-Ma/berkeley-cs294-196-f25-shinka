# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Hybrid ARC + TinyLFU-inspired sentry with adaptive window control and proactive cooling.
# Live segments:
#  - Probation (recently inserted, not yet proven)
#  - Protected (proven frequent/reused)
# Ghost metadata (no data):
#  - m_ghost_probation: keys recently evicted from probation
#  - m_ghost_protected: keys recently evicted from protected
# Adaptive controls:
#  - m_target_protected: adaptive target size for protected segment
#  - Windowed stats drive target, scan mode, and decay half-life adjustments
# Frequency:
#  - m_freq: decayed LFU score updated on hits and lightly seeded on inserts
# Admission guard:
#  - m_last_victim_score: biases against admitting weak newcomers after evicting a strong item

m_ts = dict()                  # key -> last access timestamp
m_probation = set()            # probation membership
m_protected = set()            # protected membership

m_ghost_probation = dict()     # key -> ghost timestamp
m_ghost_protected = dict()     # key -> ghost timestamp
m_ghost_strength = dict()      # key -> last known strength at eviction

m_freq = dict()                # key -> decayed frequency score

m_target_protected = None      # int: desired protected size
m_last_capacity = None         # last capacity observed

m_decay_interval = None        # decay interval in accesses
m_last_decay_access = 0        # last time decay applied

# Windowed adaptive control
m_seen_ever = set()            # to compute unique insert rate
m_win_start = 0
m_win_size = None
m_win_inserts = 0
m_win_unique = 0
m_win_promotions = 0
m_win_ghost_prob_hits = 0
m_win_ghost_prot_hits = 0
m_win_hits = 0
m_win_misses = 0
m_scan_mode = False            # require two touches before promotion during scans
m_probation_hits = dict()      # key -> hits while in probation

# Admission guard
m_last_victim_score = 0.0
m_last_victim_key = None


def _init_targets(cache_snapshot):
    global m_target_protected, m_last_capacity, m_decay_interval, m_win_size, m_win_start
    cap = cache_snapshot.capacity or max(len(cache_snapshot.cache), 1)
    if m_target_protected is None or m_last_capacity != cap:
        m_target_protected = max(1, int(cap * 0.5))
        m_last_capacity = cap
        # Default decay "half-life" ~ 2x capacity, adjusted adaptively
        m_decay_interval = max(100, int(2.0 * cap))
        # Window spans ~5x capacity accesses for robust signal
        m_win_size = max(200, int(5.0 * cap))
        m_win_start = cache_snapshot.access_count


def _oldest_key(candidates):
    return min(candidates, key=lambda k: m_ts.get(k, -1))


def _trim_ghosts(capacity):
    # Bound ghost lists and their remembered strengths
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


def _maybe_decay(cache_snapshot):
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
    # Hybrid LRFU-like priority: freq - age/cap + small protected bias
    age = now - m_ts.get(key, now)
    freq = m_freq.get(key, 0.0)
    lam = 1.0 / max(1, cap)
    bonus = 0.3 if key in m_protected else 0.0
    return freq - lam * age + bonus


def _cool_protected(now, cap):
    # Proactively demote stale protected entries with very low priority
    # Limit demotions per call to avoid heavy work
    cool_thresh = 0.0
    demotions = 0
    max_demotions = 2
    while m_protected and demotions < max_demotions:
        # Find the coldest protected
        cand = min(m_protected, key=lambda k: _priority(k, now, cap))
        if _priority(cand, now, cap) < cool_thresh:
            m_protected.discard(cand)
            m_probation.add(cand)
            demotions += 1
        else:
            break


def _maybe_roll_window(cache_snapshot):
    global m_win_start, m_win_size, m_target_protected, m_scan_mode, m_decay_interval
    now = cache_snapshot.access_count
    cap = m_last_capacity or max(1, len(cache_snapshot.cache))
    if m_win_size is None:
        return
    if now - m_win_start < m_win_size:
        return

    # Compute window metrics
    total_inserts = max(1, m_win_inserts)
    total_accesses = max(1, m_win_hits + m_win_misses)
    ghost_hits = m_win_ghost_prob_hits + m_win_ghost_prot_hits
    ghost_hit_rate = ghost_hits / total_inserts
    promotion_rate = m_win_promotions / total_inserts
    unique_insert_rate = m_win_unique / total_inserts
    recent_hit_rate = m_win_hits / total_accesses

    # Adjust protected target based on signals
    step_small = max(1, cap // 32)
    step_medium = max(1, cap // 20)

    # If protected ghosts or promotions are frequent, grow protected
    if ghost_hit_rate > 0.15 or promotion_rate > 0.25 or m_win_ghost_prot_hits > m_win_ghost_prob_hits:
        m_target_protected = min(int(0.7 * cap), m_target_protected + step_medium)
    # If unique inserts dominate and hits are scarce, shrink protected (scan defense)
    if unique_insert_rate > 0.6 and recent_hit_rate < 0.2:
        m_target_protected = max(int(0.2 * cap), m_target_protected - step_medium)

    # Determine scan mode
    m_scan_mode = (unique_insert_rate > 0.6 and recent_hit_rate < 0.2)

    # Adapt decay interval (half-life): shorter under churn/scans, longer under reuse
    target_half = int(0.8 * cap) if m_scan_mode else int(2.5 * cap)
    # Smooth update
    m_decay_interval = max(50, int(0.5 * m_decay_interval + 0.5 * target_half))

    # Reset window
    m_win_start = now
    _reset_window_counters()


def _reset_window_counters():
    global m_win_inserts, m_win_unique, m_win_promotions
    global m_win_ghost_prob_hits, m_win_ghost_prot_hits
    global m_win_hits, m_win_misses
    m_win_inserts = 0
    m_win_unique = 0
    m_win_promotions = 0
    m_win_ghost_prob_hits = 0
    m_win_ghost_prot_hits = 0
    m_win_hits = 0
    m_win_misses = 0


def evict(cache_snapshot, obj):
    '''
    Choose eviction victim.
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

    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)
    now = cache_snapshot.access_count

    # Prefer evicting from probation unless protected exceeds target
    if protected_candidates and len(m_protected) > (m_target_protected or 0):
        return min(protected_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    if probation_candidates:
        return min(probation_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    if protected_candidates:
        return min(protected_candidates, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))

    # Fallback: globally coldest
    if keys_in_cache:
        return min(keys_in_cache, key=lambda k: (_priority(k, now, cap), m_ts.get(k, -1)))
    return None


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    global m_ts, m_probation, m_protected, m_target_protected, m_freq, m_win_hits, m_win_promotions
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    now = cache_snapshot.access_count
    key = obj.key

    # Update recency and frequency
    m_ts[key] = now
    m_freq[key] = m_freq.get(key, 0.0) + 1.0
    m_win_hits += 1

    # Promotion policy: in scan mode require two touches in probation
    if key in m_probation:
        m_probation_hits[key] = m_probation_hits.get(key, 0) + 1
        promote = (m_probation_hits[key] >= (2 if m_scan_mode else 1))
        if promote:
            m_probation.discard(key)
            m_probation_hits.pop(key, None)
            m_protected.add(key)
            # Nudge protected target upward slightly on successful reuse
            cap = m_last_capacity or max(len(cache_snapshot.cache), 1)
            m_target_protected = min(cap, max(1, m_target_protected + 1))
            m_win_promotions += 1
    elif key not in m_protected:
        # If metadata was missing, ensure protected membership
        m_protected.add(key)

    # Proactively cool protected if some are stale
    _cool_protected(now, m_last_capacity or 1)


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after inserting a new object into the cache.
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected
    global m_target_protected, m_freq, m_ghost_strength, m_seen_ever
    global m_win_inserts, m_win_unique, m_win_ghost_prob_hits, m_win_ghost_prot_hits, m_win_misses
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    now = cache_snapshot.access_count
    key = obj.key
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # Record miss for window stats
    m_win_misses += 1
    m_win_inserts += 1
    if key not in m_seen_ever:
        m_seen_ever.add(key)
        m_win_unique += 1

    # ARC-like adaptation on ghost re-reference
    was_ghost_prob = key in m_ghost_probation
    was_ghost_prot = key in m_ghost_protected
    prev_strength = m_ghost_strength.pop(key, 0.0)

    step = max(1, cap // 32)
    if was_ghost_prob:
        m_target_protected = max(1, m_target_protected - step)
        m_ghost_probation.pop(key, None)
        m_win_ghost_prob_hits += 1
    elif was_ghost_prot:
        m_target_protected = min(cap, m_target_protected + step)
        m_ghost_protected.pop(key, None)
        m_win_ghost_prot_hits += 1

    # Insert into probation
    m_ts[key] = now
    m_protected.discard(key)
    m_probation.add(key)
    m_probation_hits[key] = 0

    # Admission guard seeding:
    # - If previously protected ghost, seed strongly.
    # - If probation ghost, seed moderately.
    # - If no ghost and last victim was strong, seed as 0 to prefer early eviction (TinyLFU-like guard).
    base = 0.1
    if was_ghost_prot:
        base = max(base, 1.2)
    elif was_ghost_prob:
        base = max(base, 0.6)
    elif m_last_victim_score > 2.0:
        base = 0.0  # weak admission under strong-victim pressure
    if prev_strength > 0:
        base = max(base, prev_strength * 0.7)
    m_freq[key] = base

    # Trim ghosts
    _trim_ghosts(cap)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after evicting the victim.
    '''
    global m_ts, m_probation, m_protected, m_ghost_probation, m_ghost_protected
    global m_freq, m_ghost_strength, m_last_victim_score, m_last_victim_key
    _init_targets(cache_snapshot)
    _maybe_decay(cache_snapshot)
    _maybe_roll_window(cache_snapshot)

    evk = evicted_obj.key
    now = cache_snapshot.access_count
    cap = m_last_capacity or max(len(cache_snapshot.cache), 1)

    # Capture segment and score before removal
    was_protected = evk in m_protected
    score = _priority(evk, now, cap)

    # Remove metadata
    last_strength = m_freq.pop(evk, 0.0)
    m_ts.pop(evk, None)
    m_probation.discard(evk)
    m_protected.discard(evk)
    m_probation_hits.pop(evk, None)

    # Ghost recording and strength remember
    if was_protected:
        m_ghost_protected[evk] = now
    else:
        m_ghost_probation[evk] = now
    if last_strength > 0:
        m_ghost_strength[evk] = last_strength

    # Admission guard memory
    m_last_victim_score = score
    m_last_victim_key = evk

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