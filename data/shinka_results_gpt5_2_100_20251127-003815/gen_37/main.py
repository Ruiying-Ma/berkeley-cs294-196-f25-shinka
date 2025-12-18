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

# TinyLFU-lite and victim sampling state
m_last_victim_key = None
m_sample_k = 4
m_last_decay_access = 0
m_decay_interval = 512


def _reset_if_new_run(cache_snapshot):
    """Reset metadata when a new trace/cache run starts."""
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, _m_last_seen_access
    global m_last_victim_key, m_sample_k, m_last_decay_access, m_decay_interval
    # New run if access counter restarts or at very beginning
    if cache_snapshot.access_count <= 1 or _m_last_seen_access > cache_snapshot.access_count:
        m_probation.clear()
        m_protected.clear()
        m_b1.clear()
        m_b2.clear()
        m_freq.clear()
        m_p = 0
        m_last_victim_key = None
        cap = max(int(cache_snapshot.capacity), 1)
        # sampling factor relative to capacity
        m_sample_k = max(3, min(6, cap // 16 or 3))
        # frequency aging interval proportional to capacity
        m_decay_interval = max(256, 4 * cap)
        m_last_decay_access = cache_snapshot.access_count
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


# --------- TinyLFU-lite helpers ---------
def _maybe_age_freq(cache_snapshot):
    """Periodically halve frequencies to age out stale history."""
    global m_freq, m_last_decay_access, m_decay_interval
    cap = max(int(cache_snapshot.capacity), 1)
    if m_decay_interval <= 0:
        m_decay_interval = max(256, 4 * cap)
    if cache_snapshot.access_count - m_last_decay_access >= m_decay_interval:
        to_del = []
        for k, v in m_freq.items():
            nv = v >> 1
            if nv <= 0:
                to_del.append(k)
            else:
                m_freq[k] = nv
        for k in to_del:
            m_freq.pop(k, None)
        m_last_decay_access = cache_snapshot.access_count
        # readjust interval if capacity changed
        m_decay_interval = max(256, 4 * cap)


def _freq_inc(key: str, amt: int = 1):
    v = m_freq.get(key, 0) + amt
    if v > 255:
        v = 255
    m_freq[key] = v


def _pick_sampled_lru_min_freq(od: OrderedDict, sample_k: int) -> str:
    """Sample a few LRU-side candidates and pick the one with lowest frequency."""
    if not od:
        return None
    k = min(sample_k, len(od))
    it = iter(od.keys())
    min_key = None
    min_f = None
    for _ in range(k):
        key = next(it)
        f = m_freq.get(key, 0)
        if min_f is None or f < min_f:
            min_f = f
            min_key = key
    if min_key is None:
        min_key = next(iter(od))
    return min_key


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, m_sample_k
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    # Seed metadata if empty by placing all current cache keys into probation.
    if not m_probation and not m_protected and cache_snapshot.cache:
        for k0 in cache_snapshot.cache.keys():
            m_probation[k0] = None

    cap = max(int(cache_snapshot.capacity), 1)
    # adapt sampling to capacity
    m_sample_k = max(3, min(6, cap // 16 or 3))

    # ARC: adjust target m_p based on ghost hit of incoming key
    k_new = obj.key
    if k_new in m_b1:
        delta = max(1, len(m_b2) // max(1, len(m_b1)))
        m_p = min(cap, m_p + delta)
    elif k_new in m_b2:
        delta = max(1, len(m_b1) // max(1, len(m_b2)))
        m_p = max(0, m_p - delta)

    # ARC replacement decision (do not mutate structures here)
    choose_T1 = bool(m_probation) and ((k_new in m_b2 and len(m_probation) == m_p) or (len(m_probation) > m_p))

    candid_obj_key = None
    if choose_T1 and m_probation:
        # Evict from probation: sample LRU side and pick lowest frequency
        candid_obj_key = _pick_sampled_lru_min_freq(m_probation, m_sample_k)
    elif m_protected:
        # Prefer evicting from T1 when it has an equally cold candidate
        t2_victim = _pick_sampled_lru_min_freq(m_protected, m_sample_k)
        if m_probation:
            t1_victim = _pick_sampled_lru_min_freq(m_probation, m_sample_k)
            f_t2 = m_freq.get(t2_victim, 0)
            f_t1 = m_freq.get(t1_victim, 0)
            # If T1 is not hotter than T2, choose T1 victim to protect hot set
            if f_t1 + 1 <= f_t2:
                candid_obj_key = t1_victim
            else:
                candid_obj_key = t2_victim
        else:
            candid_obj_key = t2_victim
    elif m_probation:
        candid_obj_key = _pick_sampled_lru_min_freq(m_probation, m_sample_k)
    else:
        # Fallback: choose any key from the cache
        for k_any in cache_snapshot.cache.keys():
            candid_obj_key = k_any
            break
    return candid_obj_key


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    _maybe_age_freq(cache_snapshot)
    k = obj.key
    _freq_inc(k, 1)

    if k in m_protected:
        # Refresh recency in protected
        m_protected.move_to_end(k, last=True)
    elif k in m_probation:
        # Promote to protected on hit
        m_probation.pop(k, None)
        m_protected[k] = None
    else:
        # Metadata miss but cache hit: treat as hot and place into protected
        m_protected[k] = None

    # Keep protected list within target by demoting its LRU into probation
    prot_tgt = _protected_target_size(cache_snapshot)
    if len(m_protected) > prot_tgt:
        dem_k, _ = m_protected.popitem(last=False)
        # Demote to probation (recency segment)
        m_probation.pop(dem_k, None)
        m_probation[dem_k] = None


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_probation, m_protected, m_b1, m_b2, m_freq, m_p, m_last_victim_key
    _reset_if_new_run(cache_snapshot)
    _prune_metadata(cache_snapshot)

    _maybe_age_freq(cache_snapshot)
    k = obj.key
    # Remove any stale placements
    m_protected.pop(k, None)
    m_probation.pop(k, None)

    # Frequency credit on insertion (TinyLFU counts misses too)
    _freq_inc(k, 1)

    cap = max(int(cache_snapshot.capacity), 1)

    # ARC adaptation on ghost hits; insert to protected if from ghosts
    if k in m_b1:
        delta = max(1, len(m_b2) // max(1, len(m_b1)))
        m_p = min(cap, m_p + delta)
        m_b1.pop(k, None)
        m_protected[k] = None
    elif k in m_b2:
        delta = max(1, len(m_b1) // max(1, len(m_b2)))
        m_p = max(0, m_p - delta)
        m_b2.pop(k, None)
        m_protected[k] = None
    else:
        # New key: admission decision using TinyLFU-lite and last victim comparison
        hot_thresh = 3
        new_f = m_freq.get(k, 0)
        vict_f = m_freq.get(m_last_victim_key, -1) if m_last_victim_key is not None else -1
        if new_f >= hot_thresh and new_f >= vict_f:
            m_protected[k] = None
        else:
            m_probation[k] = None

    # Keep protected within target by demoting its LRU into probation
    prot_tgt = _protected_target_size(cache_snapshot)
    if len(m_protected) > prot_tgt:
        dem_k, _ = m_protected.popitem(last=False)
        m_probation.pop(dem_k, None)
        m_probation[dem_k] = None

    # Bound ghost sizes to capacity
    while len(m_b1) > cap:
        m_b1.popitem(last=False)
    while len(m_b2) > cap:
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
    global m_probation, m_protected, m_b1, m_b2, m_last_victim_key
    _reset_if_new_run(cache_snapshot)
    # Record the last victim for admission comparison
    m_last_victim_key = evicted_obj.key

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
        # Unknown segment: default to recency ghost (B1)
        m_b1.pop(k, None)
        m_b1[k] = None

    # Bound ghost sizes to capacity
    cap = max(int(cache_snapshot.capacity), 1)
    while len(m_b1) > cap:
        m_b1.popitem(last=False)
    while len(m_b2) > cap:
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