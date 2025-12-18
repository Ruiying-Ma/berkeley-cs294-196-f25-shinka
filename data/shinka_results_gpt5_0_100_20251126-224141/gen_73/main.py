# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads
W-TinyLFU-inspired SLRU with aging frequency sketch.
"""

from collections import OrderedDict

# Global metadata
arc_capacity = None  # reuse name for capacity
m_key_timestamp = dict()   # per-key last access time (for tie-breaking)
m_key_freq = dict()        # lightweight frequency sketch (decayed)

# Segmented LRU state (resident only)
slru_prob = OrderedDict()  # probation segment (recent admissions)
slru_prot = OrderedDict()  # protected segment (repeatedly used)

# Global LRU for deterministic fallback and drift repair
all_lru = OrderedDict()

# SLRU sizing and aging control
prot_ratio = 0.8           # fraction of capacity dedicated to protected segment
prot_target = 0            # computed from capacity
last_age_access = 0
age_interval = 64          # will scale with capacity


def _ensure_capacity(cache_snapshot):
    global arc_capacity, prot_target, age_interval
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)
        prot_target = max(1, int(arc_capacity * prot_ratio))
        age_interval = max(arc_capacity, 64)


def _move_to_mru(od, key):
    if key in od:
        od.pop(key, None)
    od[key] = True


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _age_freq(now):
    # Periodically halve all frequency counters to adapt to phase changes
    global last_age_access
    if now - last_age_access < age_interval:
        return
    last_age_access = now
    to_del = []
    for k, c in m_key_freq.items():
        nc = c // 2
        if nc <= 0:
            to_del.append(k)
        else:
            m_key_freq[k] = nc
    for k in to_del:
        m_key_freq.pop(k, None)


def _resync(cache_snapshot):
    # Ensure resident metadata matches actual cache content
    cache_keys = set(cache_snapshot.cache.keys())

    # Remove keys no longer resident
    for od in (slru_prob, slru_prot, all_lru):
        for k in list(od.keys()):
            if k not in cache_keys:
                od.pop(k, None)

    # Add any missing resident keys conservatively to probation
    for k in cache_keys:
        if k not in slru_prob and k not in slru_prot:
            slru_prob[k] = True
        if k not in all_lru:
            all_lru[k] = True


def _enforce_slru_sizes():
    # Demote protected overflow into probation to keep protected bounded
    while len(slru_prot) > prot_target:
        demote = _pop_lru(slru_prot)
        if demote is not None:
            _move_to_mru(slru_prob, demote)


def _freq(key):
    return m_key_freq.get(key, 0)


def _pick_victim_from_od(od, now, depth=4):
    # Among the first 'depth' LRU entries, pick the lowest-frequency and oldest
    if not od:
        return None
    best_k = None
    best_tuple = None  # (freq, timestamp)
    i = 0
    for k in od.keys():
        i += 1
        f = _freq(k)
        ts = m_key_timestamp.get(k, 0)
        tup = (f, ts)  # lower freq first; for tie, older (smaller ts) first
        if best_tuple is None or tup < best_tuple:
            best_tuple = tup
            best_k = k
        if i >= depth:
            break
    return best_k


def evict(cache_snapshot, obj):
    '''
    Choose an eviction victim.
    Prefer eviction from probation (SLRU), with LFU-biased sampling.
    Fall back to protected only if probation is empty.
    '''
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    now = cache_snapshot.access_count
    _age_freq(now)
    _enforce_slru_sizes()

    # Depth based on capacity for more stable choices
    depth = min(8, max(1, arc_capacity // 16))

    victim = None
    if slru_prob:
        victim = _pick_victim_from_od(slru_prob, now, depth)
        if victim is None:
            victim = next(iter(slru_prob))  # LRU fallback
    elif slru_prot:
        # Protected only when probation is empty
        victim = _pick_victim_from_od(slru_prot, now, depth)
        if victim is None:
            victim = next(iter(slru_prot))
    else:
        # As a last resort, evict the global LRU from snapshot
        if cache_snapshot.cache:
            victim = next(iter(cache_snapshot.cache.keys()))
    return victim


def update_after_hit(cache_snapshot, obj):
    '''
    On hit: increment frequency, update timestamps/LRU; promote from probation to protected.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    _age_freq(now)
    _resync(cache_snapshot)

    k = obj.key
    # Update global LRU and timestamp
    _move_to_mru(all_lru, k)
    m_key_timestamp[k] = now
    # Increment frequency with small saturation
    m_key_freq[k] = min(255, m_key_freq.get(k, 0) + 1)

    if k in slru_prob:
        # Promote to protected on first hit
        slru_prob.pop(k, None)
        _move_to_mru(slru_prot, k)
    elif k in slru_prot:
        # Refresh recency within protected
        _move_to_mru(slru_prot, k)
    else:
        # Drift repair: place into probation if somehow missing
        _move_to_mru(slru_prob, k)

    _enforce_slru_sizes()


def update_after_insert(cache_snapshot, obj):
    '''
    On insert (after a miss): add to probation MRU, seed frequency and timestamp.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    _age_freq(now)
    _resync(cache_snapshot)

    k = obj.key
    # New admissions enter probation
    _move_to_mru(slru_prob, k)
    _move_to_mru(all_lru, k)
    m_key_timestamp[k] = now
    # Seed a small initial frequency; keep cumulative count across evictions for TinyLFU effect
    m_key_freq[k] = min(255, m_key_freq.get(k, 0) + 1)

    _enforce_slru_sizes()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After eviction: remove the victim from SLRU segments and global LRU.
    Keep frequency counts to preserve TinyLFU knowledge across re-admissions.
    '''
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)

    k = evicted_obj.key
    # Remove from resident segments and global LRU
    slru_prob.pop(k, None)
    slru_prot.pop(k, None)
    all_lru.pop(k, None)
    # Keep m_key_freq[k] to preserve history (decay will prune when it cools)
    m_key_timestamp.pop(k, None)

    _enforce_slru_sizes()
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