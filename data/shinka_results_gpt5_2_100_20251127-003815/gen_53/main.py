# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# Adaptive Replacement Cache (ARC)-inspired:
# - m_probation (T1): recent entries (LRU)
# - m_protected (T2): frequent/hot entries (LRU)
# - m_b1 (B1): ghost list of keys evicted from T1 (LRU)
# - m_b2 (B2): ghost list of keys evicted from T2 (LRU)
# - m_p: adaptive target size for T1
m_probation = OrderedDict()
m_protected = OrderedDict()
m_b1 = OrderedDict()
m_b2 = OrderedDict()
m_freq = dict()
m_p = 0
_m_last_seen_access = -1  # detect new traces to reset metadata

# Sampling and light frequency aging
m_sample_k = 6
_age_ops = 0
_age_period = 1024
m_miss_streak = 0


def _reset_if_new_run(cache_snapshot):
    """Reset metadata when a new trace/cache run starts."""
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, _m_last_seen_access, m_sample_k, _age_ops, _age_period, m_miss_streak
    # New run if access counter restarts or at very beginning
    if cache_snapshot.access_count <= 1 or _m_last_seen_access > cache_snapshot.access_count:
        m_probation.clear()
        m_protected.clear()
        m_b1.clear()
        m_b2.clear()
        m_freq.clear()
        m_p = 0
        _age_ops = 0
        m_miss_streak = 0
    # keep aging period and sample size responsive to capacity
    cap = max(int(cache_snapshot.capacity), 1)
    _age_period = max(512, cap * 8)
    m_sample_k = max(4, min(12, (cap // 8) or 4))
    _m_last_seen_access = cache_snapshot.access_count


def _prune_metadata(cache_snapshot):
    """Keep metadata consistent with actual cache content."""
    cache_keys = cache_snapshot.cache.keys()
    for seg in (m_probation, m_protected):
        to_del = [k for k in seg.keys() if k not in cache_keys]
        for k in to_del:
            seg.pop(k, None)


def _protected_target_size(cache_snapshot):
    """Aim to keep most entries protected while leaving room in probation."""
    cap = max(int(cache_snapshot.capacity), 1)
    return max(1, int(cap * 0.8))


def _maybe_age_freq(cache_snapshot):
    """Periodically halve frequency counters to forget stale history."""
    global _age_ops, _age_period, m_freq
    _age_ops += 1
    if _age_ops % max(1, _age_period) == 0:
        for k in list(m_freq.keys()):
            v = m_freq.get(k, 0) >> 1
            if v <= 0:
                # keep dictionary small
                m_freq.pop(k, None)
            else:
                m_freq[k] = v


def _sample_lowfreq(od: OrderedDict) -> str:
    """Sample a few keys from the LRU side and pick the lowest-frequency candidate."""
    if not od:
        return None
    k = min(m_sample_k, len(od))
    it = iter(od.keys())  # LRU -> MRU
    best_k = None
    best_f = None
    for _ in range(k):
        key = next(it)
        f = m_freq.get(key, 0)
        if best_f is None or f < best_f:
            best_f = f
            best_k = key
    return best_k if best_k is not None else next(iter(od))


def _enforce_protected_target(cache_snapshot):
    """Demote LRU of protected to probation until protected meets its target."""
    target = _protected_target_size(cache_snapshot)
    while len(m_protected) > target:
        demote_k, _ = m_protected.popitem(last=False)  # LRU of protected
        # place demoted key at MRU of probation to avoid immediate eviction
        m_probation[demote_k] = None
        m_probation.move_to_end(demote_k, last=True)


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, m_miss_streak
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    # Seed metadata if empty by placing all current cache keys into probation.
    if not m_probation and not m_protected and cache_snapshot.cache:
        for k0 in cache_snapshot.cache.keys():
            m_probation[k0] = None

    cap = max(int(cache_snapshot.capacity), 1)
    k = obj.key

    # ARC: adjust target p on ghost hits of incoming key before replacement
    if k in m_b1:
        delta = max(1, len(m_b2) // max(1, len(m_b1)))
        m_p = min(cap, m_p + delta)
    elif k in m_b2:
        delta = max(1, len(m_b1) // max(1, len(m_b2)))
        m_p = max(0, m_p - delta)

    # Simple scan guard: during prolonged miss streaks, favor evicting from probation (T1)
    scan_pressure = m_miss_streak > (cap // 2)

    # ARC replacement decision (do not mutate structures here)
    if scan_pressure and m_probation:
        candid_obj_key = _sample_lowfreq(m_probation)
    elif m_probation and ((k in m_b2 and len(m_probation) == m_p) or (len(m_probation) > m_p)):
        # Evict low-frequency among LRU tail of probation (T1)
        candid_obj_key = _sample_lowfreq(m_probation)
    elif m_protected:
        # Evict low-frequency among LRU tail of protected (T2)
        candid_obj_key = _sample_lowfreq(m_protected)
    elif m_probation:
        candid_obj_key = _sample_lowfreq(m_probation)
    else:
        # Fallback: choose any key from the cache
        candid_obj_key = next(iter(cache_snapshot.cache))
    return candid_obj_key


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_miss_streak, m_p
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)
    _maybe_age_freq(cache_snapshot)

    k = obj.key
    cap = max(int(cache_snapshot.capacity), 1)

    # Bump lightweight frequency
    m_freq[k] = m_freq.get(k, 0) + 1

    # Gentle cooldown of miss streak to support scan guard decay
    m_miss_streak = max(0, m_miss_streak - 2)
    scan_pressure = m_miss_streak > (cap // 2)

    if k in m_protected:
        # Refresh recency in protected
        m_protected.move_to_end(k, last=True)
    elif k in m_probation:
        # Promote on second touch unless under scan pressure
        if not scan_pressure and m_freq.get(k, 0) >= 2:
            m_probation.pop(k, None)
            m_protected[k] = None
        else:
            # Refresh recency in probation
            m_probation.move_to_end(k, last=True)
    else:
        # Metadata miss but cache hit: treat as hot and place into protected
        # unless scan pressure is extreme (we still allow, bounded by T2 target)
        m_protected[k] = None

    # Enforce ARC-like target for protected: |T2| <= c - p
    target_t2 = max(0, cap - m_p)
    while len(m_protected) > target_t2:
        demote_k, _ = m_protected.popitem(last=False)  # LRU of protected
        m_probation[demote_k] = None
        m_probation.move_to_end(demote_k, last=True)


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_miss_streak, m_p
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)
    _maybe_age_freq(cache_snapshot)

    k = obj.key
    cap = max(int(cache_snapshot.capacity), 1)

    # Remove any stale placements
    m_protected.pop(k, None)
    m_probation.pop(k, None)

    # ARC admission: if key was in ghosts, insert to protected (T2); else to probation (T1)
    if k in m_b1:
        m_b1.pop(k, None)
        m_protected[k] = None
    elif k in m_b2:
        m_b2.pop(k, None)
        m_protected[k] = None
    else:
        m_probation[k] = None

    # Light frequency credit and miss streak for scan detection (bounded)
    m_freq[k] = m_freq.get(k, 0) + 1
    m_miss_streak = min(m_miss_streak + 1, cap * 4)

    # Enforce ARC-like target for protected: |T2| <= c - p
    target_t2 = max(0, cap - m_p)
    while len(m_protected) > target_t2:
        demote_k, _ = m_protected.popitem(last=False)  # LRU of protected
        m_probation[demote_k] = None
        m_probation.move_to_end(demote_k, last=True)

    # Bound combined ghost sizes to capacity
    while (len(m_b1) + len(m_b2)) > cap:
        if len(m_b1) > len(m_b2):
            m_b1.popitem(last=False)
        else:
            m_b2.popitem(last=False)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2
    _reset_if_new_run(cache_snapshot)
    # Move evicted object into appropriate ghost list
    k = evicted_obj.key
    if k in m_probation:
        m_probation.pop(k, None)
        m_b1.pop(k, None)
        m_b1[k] = None  # MRU of B1
    elif k in m_protected:
        m_protected.pop(k, None)
        m_b2.pop(k, None)
        m_b2[k] = None  # MRU of B2
    else:
        # No segment info; do nothing
        pass

    # Bound combined ghost sizes to capacity
    cap = max(int(cache_snapshot.capacity), 1)
    while (len(m_b1) + len(m_b2)) > cap:
        if len(m_b1) > len(m_b2):
            m_b1.popitem(last=False)
        else:
            m_b2.popitem(last=False)

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