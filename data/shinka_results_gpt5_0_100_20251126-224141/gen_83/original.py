# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LRU timestamp map kept for tie-breaking and fallback
m_key_timestamp = dict()

# Tiny-LFU style lightweight frequency map (resident keys only)
m_key_freq = dict()
last_freq_decay_access = 0

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
# Short window to bias REPLACE during detected scans
scan_guard_until = -1
# One-time extra clamp flag during cold scan phases
cold_extra_applied = False


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
    # Keep ghosts total size within capacity with p-aware balancing
    total = len(arc_B1) + len(arc_B2)
    C = arc_capacity if arc_capacity is not None else 1
    # Per-side targets based on current p
    target_B1 = min(C, max(0, arc_p))
    target_B2 = max(0, C - target_B1)
    while total > C:
        # Prefer trimming the side that exceeds its target
        if len(arc_B1) > target_B1 and arc_B1:
            _pop_lru(arc_B1)
        elif len(arc_B2) > target_B2 and arc_B2:
            _pop_lru(arc_B2)
        else:
            # Otherwise trim from the larger side
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
    # Proportional, bounded decay of p when no ghost hits for a while
    global arc_p, cold_extra_applied
    if last_ghost_hit_access >= 0 and arc_p > 0:
        idle = cache_snapshot.access_count - last_ghost_hit_access
        C = arc_capacity if arc_capacity else 1
        if idle > 0:
            # Decay step grows with idle time but is capped to avoid oscillation
            cap_step = max(1, C // 8)
            dyn_step = max(1, idle // max(1, C // 4))
            step = min(cap_step, dyn_step)
            arc_p = max(0, arc_p - step)
    # One-time extra clamp during prolonged cold streaks (scan-like)
    C = arc_capacity if arc_capacity else 1
    if cold_streak >= max(1, C // 2) and not cold_extra_applied:
        extra = min(max(1, C // 4), max(1, cold_streak // max(1, C // 8)))
        arc_p = max(0, arc_p - extra)
        cold_extra_applied = True


def _maybe_decay_freq(cache_snapshot):
    # Periodically decay frequency for cached keys to keep LFU signal fresh and bounded
    global last_freq_decay_access
    _ensure_capacity(cache_snapshot)
    C = arc_capacity if arc_capacity else 1
    interval = max(64, C)  # decay cadence: per-cache-sized accesses at least
    if cache_snapshot.access_count - last_freq_decay_access >= interval:
        for k in list(cache_snapshot.cache.keys()):
            if k in m_key_freq and m_key_freq[k] > 0:
                m_key_freq[k] -= 1
                if m_key_freq[k] == 0:
                    # keep entry minimal; optional cleanup
                    m_key_freq.pop(k, None)
        last_freq_decay_access = cache_snapshot.access_count


def _pick_freq_aware_lru(od, limit):
    # Among the LRU-side window of 'od', pick the item with lexicographically minimal (freq, timestamp)
    best_k = None
    best_tuple = None
    count = 0
    for k in od.keys():
        f = m_key_freq.get(k, 0)
        ts = m_key_timestamp.get(k, 0)
        cand = (f, ts)
        if best_tuple is None or cand < best_tuple:
            best_tuple = cand
            best_k = k
        count += 1
        if count >= limit:
            break
    if best_k is None and od:
        # fallback to pure LRU
        best_k = next(iter(od))
    return best_k


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global arc_p, last_ghost_hit_access, cold_streak, scan_guard_until, cold_extra_applied
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    # Ghost-driven p updates BEFORE REPLACE (canonical ARC)
    key = obj.key
    C = arc_capacity if arc_capacity else 1
    in_B1 = key in arc_B1
    in_B2 = key in arc_B2
    if in_B1:
        step = max(1, len(arc_B2) // max(1, len(arc_B1)))
        arc_p = min(C, arc_p + min(step, max(1, C // 8)))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        scan_guard_until = -1  # ghost hit: cancel scan guard
        cold_extra_applied = False
    elif in_B2:
        step = max(1, len(arc_B1) // max(1, len(arc_B2)))
        dec_cap = max(1, (C // 4) if cold_streak >= max(1, C // 2) else (C // 8))
        arc_p = max(0, arc_p - min(step, dec_cap))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        scan_guard_until = -1
        cold_extra_applied = False
    else:
        # Brand-new key: during scan streaks, gently bias toward smaller p and open guard window
        if cold_streak >= max(1, C // 2):
            arc_p = max(0, arc_p - max(1, C // 8))
            scan_guard_until = max(scan_guard_until, cache_snapshot.access_count + max(1, C // 8))

    # ARC REPLACE with guard-adjusted effective p
    t1_sz = len(arc_T1)
    guard_active = (scan_guard_until != -1 and cache_snapshot.access_count < scan_guard_until)
    p_eff = max(0, arc_p - (max(1, C // 8) if guard_active else 0))

    # Frequency-aware victim selection within the chosen list
    window = max(1, C // 8)
    candidate = None
    if t1_sz >= 1 and (t1_sz > p_eff or (in_B2 and t1_sz == p_eff)):
        # Evict from T1: choose low-frequency, old among LRU-side window
        candidate = _pick_freq_aware_lru(arc_T1, window)
    else:
        # Evict from T2: choose low-frequency, old among LRU-side window
        candidate = _pick_freq_aware_lru(arc_T2, window)

    # Strengthened, ghost-informed deterministic fallback selection
    if candidate is None:
        # 1) Prefer T1 LRU not hinted as frequent (not in B2) - residents should be disjoint, but keep for robustness
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
        # 3) Depth-limited peek to avoid B2-hinted keys and prefer B1-hinted in T2
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
        # 4) Timestamp tie-breaker restricted to T1 keys first
        min_ts = float('inf')
        min_k = None
        for k in arc_T1.keys():
            ts = m_key_timestamp.get(k, float('inf'))
            if ts < min_ts:
                min_ts = ts
                min_k = k
        candidate = min_k
    if candidate is None and m_key_timestamp:
        # 5) Fallback timestamp across all cached keys
        min_ts = float('inf')
        min_k = None
        for k in cache_snapshot.cache.keys():
            ts = m_key_timestamp.get(k, float('inf'))
            if ts < min_ts:
                min_ts = ts
                min_k = k
        candidate = min_k
    if candidate is None and cache_snapshot.cache:
        # 6) Last resort: arbitrary
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
    global m_key_timestamp, cold_streak, scan_guard_until
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

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
    # Any hit breaks a cold streak and cancels scan guard
    cold_streak = 0
    scan_guard_until = -1
    # Update timestamp and frequency for tie-breaking/fallback
    m_key_timestamp[key] = cache_snapshot.access_count
    m_key_freq[key] = m_key_freq.get(key, 0) + 1


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, cold_streak, scan_guard_until
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    key = obj.key
    # ARC admission policy: ghost hits go to T2 (p already adjusted in evict)
    if key in arc_B1 or key in arc_B2:
        cold_streak = 0
        scan_guard_until = -1
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)  # keep ghosts disjoint
        _move_to_mru(arc_T2, key)
        # Ghost hits imply this key has history; seed a higher freq
        m_key_freq[key] = m_key_freq.get(key, 0) + 2
    else:
        # Brand new: insert into T1; during scans, insert at LRU and open guard
        cold_streak += 1
        if cold_streak >= max(1, arc_capacity // 2):
            _insert_at_lru(arc_T1, key)
            scan_guard_until = max(scan_guard_until, cache_snapshot.access_count + max(1, arc_capacity // 8))
        else:
            _move_to_mru(arc_T1, key)
        # Ensure ghosts are disjoint from residents
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        # Seed minimal frequency for brand new
        m_key_freq[key] = m_key_freq.get(key, 0) + 1

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
    # Remove timestamp and frequency entry for evicted item to avoid growth
    m_key_timestamp.pop(k, None)
    m_key_freq.pop(k, None)
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