# EVOLVE-BLOCK-START
"""Cache eviction algorithm: SLRU with TinyLFU admission-style victim ranking and adaptive protected ratio"""

from collections import OrderedDict

# -------------------------------
# Global metadata (SLRU + TinyLFU)
# -------------------------------

# Segmented LRU
slru_S0 = OrderedDict()  # probationary (recency-tested)
slru_S1 = OrderedDict()  # protected (frequently-hit)
ghost_S0 = OrderedDict()  # ghost history for S0 evictions
ghost_S1 = OrderedDict()  # ghost history for S1 evictions

# Target size for protected segment (adapts online)
target_S1 = 0
slru_capacity = None

# Timestamps for tie-breaking
m_key_timestamp = dict()

# Cold streak detection for scan guard/pollution control
cold_miss_streak = 0  # consecutive brand-new inserts (not ghost hits)

# Last eviction source segment (for robust ghosting)
last_evicted_from_segment = None  # 'S0' or 'S1' or None

# -------------------------------
# TinyLFU Count-Min Sketch (CMS)
# -------------------------------
cms_width = 0
cms_counts = []
cms_hash_seeds = (0x9e3779b1, 0x7f4a7c15, 0x94d049bb, 0x27d4eb2d)  # distinct odd constants
cms_sampled = 0
cms_age_period = 0  # how often to age
cms_aged_at = 0     # access count at last aging


def _ensure_capacity(cache_snapshot):
    global slru_capacity, target_S1, cms_width, cms_counts, cms_age_period
    if slru_capacity is None:
        # Treat capacity as number of objects (the framework uses unit-sized objects)
        slru_capacity = max(int(cache_snapshot.capacity), 1)
        # Initialize protected target to half
        target_S1 = slru_capacity // 2
        # Init TinyLFU CMS: width ~ 4x capacity, power of two for fast masking
        width = 1
        desired = max(64, 4 * slru_capacity)
        while width < desired:
            width <<= 1
        cms_width = width
        cms_counts = [0] * cms_width
        cms_age_period = max(512, 8 * slru_capacity)  # periodic aging
        # Reset ghosts
        ghost_S0.clear()
        ghost_S1.clear()


def _hash_index(key_str, seed):
    # Stable within run; mask to width
    h = hash((key_str, seed))
    if h < 0:
        h = -h
    return h & (cms_width - 1)


def _cms_add(key_str, delta=1):
    if cms_width == 0:
        return
    for s in cms_hash_seeds:
        idx = _hash_index(key_str, s)
        val = cms_counts[idx] + delta
        # Avoid unbounded growth; clamp counters
        cms_counts[idx] = val if val < 0xFFFF else 0xFFFF


def _cms_estimate(key_str):
    if cms_width == 0:
        return 0
    mn = None
    for s in cms_hash_seeds:
        idx = _hash_index(key_str, s)
        v = cms_counts[idx]
        mn = v if mn is None else (v if v < mn else mn)
    return mn if mn is not None else 0


def _cms_maybe_age(access_count):
    global cms_aged_at
    if cms_width == 0:
        return
    if access_count - cms_aged_at >= cms_age_period:
        # Age the sketch by halving counters
        for i in range(cms_width):
            cms_counts[i] >>= 1
        cms_aged_at = access_count


def _move_to_mru(od, key):
    if key in od:
        od.pop(key, None)
    od[key] = True


def _insert_at_lru(od, key):
    if key in od:
        od.pop(key, None)
    od[key] = True
    try:
        od.move_to_end(key, last=False)
    except Exception:
        pass


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _rebalance_segments():
    # Keep protected segment near target by demoting from its LRU if too large.
    # We do not force S0 size; eviction will primarily reduce S0.
    while len(slru_S1) > target_S1:
        k = _pop_lru(slru_S1)
        if k is None:
            break
        _move_to_mru(slru_S0, k)


def _trim_ghosts():
    # Keep total ghosts bounded by capacity to maintain a meaningful history signal
    total = len(ghost_S0) + len(ghost_S1)
    C = slru_capacity if slru_capacity else 1
    while total > C:
        # Prefer trimming the larger ghost side
        if len(ghost_S0) >= len(ghost_S1):
            _pop_lru(ghost_S0)
        else:
            _pop_lru(ghost_S1)
        total = len(ghost_S0) + len(ghost_S1)


def _resync(cache_snapshot):
    # Synchronize SLRU sets with actual cache content
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(slru_S0.keys()):
        if k not in cache_keys:
            slru_S0.pop(k, None)
    for k in list(slru_S1.keys()):
        if k not in cache_keys:
            slru_S1.pop(k, None)
    # Any cached key not in our structures: place into S0 (probationary)
    for k in cache_keys:
        if k not in slru_S0 and k not in slru_S1:
            _move_to_mru(slru_S0, k)
    # Ensure ghosts don't contain resident keys
    for k in list(ghost_S0.keys()):
        if k in cache_keys:
            ghost_S0.pop(k, None)
    for k in list(ghost_S1.keys()):
        if k in cache_keys:
            ghost_S1.pop(k, None)
    _rebalance_segments()
    _trim_ghosts()


def _adjust_target_on_ghost(key):
    # Adjust protected target based on which ghost list contains the key
    global target_S1, cold_miss_streak
    C = slru_capacity if slru_capacity else 1
    if key in ghost_S0:
        # We evicted from S0 before; recency-only miss suggests protected too small
        step = max(1, len(ghost_S1) // max(1, len(ghost_S0)))
        target_S1 = min(C, target_S1 + step)
        # Ghost consumed
        ghost_S0.pop(key, None)
        cold_miss_streak = 0
        return True
    if key in ghost_S1:
        # We evicted from S1 before; protected likely too large
        step = max(1, len(ghost_S0) // max(1, len(ghost_S1)))
        target_S1 = max(0, target_S1 - step)
        ghost_S1.pop(key, None)
        cold_miss_streak = 0
        return True
    return False


def _select_victim(C):
    # Prefer evicting from S0; if empty, fall back to S1.
    # Use sampled TinyLFU over oldest candidates; tie-break by timestamp (older first).
    kS0 = min(8, max(1, C // 16))
    kS1 = min(2, max(1, C // 32))
    candidates = []

    # Collect S0 candidates (oldest first)
    cnt = 0
    for k in slru_S0.keys():
        candidates.append((k, 'S0'))
        cnt += 1
        if cnt >= kS0:
            break

    # If S0 empty, allow a few S1 candidates
    if not candidates:
        cnt = 0
        for k in slru_S1.keys():
            candidates.append((k, 'S1'))
            cnt += 1
            if cnt >= max(kS0, kS1):
                break
    else:
        # Also consider a tiny set from S1 in case of severely cold S0
        cnt = 0
        for k in slru_S1.keys():
            candidates.append((k, 'S1'))
            cnt += 1
            if cnt >= kS1:
                break

    # Score: frequency; prefer S0 on ties; break ties by oldest timestamp
    best = None
    best_score = None
    best_ts = None
    for k, seg in candidates:
        freq = _cms_estimate(k)
        # Penalize evicting protected segment slightly to prefer S0 unless significantly colder
        if seg == 'S1':
            freq += 1  # small bias
        ts = m_key_timestamp.get(k, 0)
        if best is None or freq < best_score or (freq == best_score and (seg == 'S0' and best[1] == 'S1')) or (freq == best_score and seg == best[1] and ts < best_ts):
            best = (k, seg)
            best_score = freq
            best_ts = ts

    return best  # (key, segment) or None


def evict(cache_snapshot, obj):
    '''
    Choose the eviction victim.
    - Return: candid_obj_key
    '''
    global last_evicted_from_segment
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _cms_maybe_age(cache_snapshot.access_count)

    C = slru_capacity if slru_capacity else 1

    # Choose victim using sampled TinyLFU preferences
    choice = _select_victim(C)
    if choice is None:
        # Fallback: any cached key (should not happen often)
        if cache_snapshot.cache:
            k = next(iter(cache_snapshot.cache.keys()))
            last_evicted_from_segment = 'S0' if k in slru_S0 else ('S1' if k in slru_S1 else None)
            return k
        return None

    k, seg = choice
    last_evicted_from_segment = seg
    return k


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    _ensure_capacity(cache_snapshot)
    _cms_maybe_age(cache_snapshot.access_count)

    key = obj.key

    # Record frequency and timestamp
    _cms_add(key, 1)
    m_key_timestamp[key] = cache_snapshot.access_count

    # Promotion/refresh
    if key in slru_S0:
        # Promote to protected MRU
        slru_S0.pop(key, None)
        _move_to_mru(slru_S1, key)
    else:
        # Refresh in protected; if absent due to drift, insert to protected
        _move_to_mru(slru_S1, key)

    # Recent hits imply frequency; gently bias towards larger protected segment
    global target_S1, cold_miss_streak
    target_S1 = min(slru_capacity, target_S1 + 1)
    cold_miss_streak = 0

    # Keep segments balanced
    _rebalance_segments()


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata immediately after inserting a new object into the cache.
    '''
    _ensure_capacity(cache_snapshot)
    _cms_maybe_age(cache_snapshot.access_count)

    key = obj.key
    C = slru_capacity if slru_capacity else 1

    # Frequency and timestamp for the accessed key (miss)
    _cms_add(key, 1)
    m_key_timestamp[key] = cache_snapshot.access_count

    ghost_hit = _adjust_target_on_ghost(key)

    # Insert new key into S0 normally; on ghost_S1 hit, allow direct placement to S1
    global cold_miss_streak
    if ghost_hit and key not in slru_S0 and key not in slru_S1:
        # If the key was in protected ghost, it likely deserves protected insertion
        # Otherwise, it goes to probationary
        if key in slru_S0 or key in slru_S1:
            pass  # already handled
        if key not in ghost_S0 and key not in ghost_S1:
            # No longer in ghost after _adjust_target_on_ghost
            pass
        # Heuristic: if S1 is not over target, place directly into S1
        if len(slru_S1) < max(1, target_S1):
            _move_to_mru(slru_S1, key)
        else:
            _move_to_mru(slru_S0, key)
        cold_miss_streak = 0
    else:
        # Brand-new miss: insert into S0; if many consecutive brand-new misses, insert at LRU to reduce pollution
        cold_miss_streak += 1
        guard_threshold = max(2, C // 4)
        if cold_miss_streak >= guard_threshold:
            _insert_at_lru(slru_S0, key)
            # During cold scans, bias target_S1 downward a bit
            global target_S1
            target_S1 = max(0, target_S1 - 1)
        else:
            _move_to_mru(slru_S0, key)

    _rebalance_segments()
    _trim_ghosts()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata immediately after evicting the victim.
    '''
    k = evicted_obj.key

    # Remove from segments and send to matching ghost
    if k in slru_S0:
        slru_S0.pop(k, None)
        _move_to_mru(ghost_S0, k)
        ghost_S1.pop(k, None)
    elif k in slru_S1:
        slru_S1.pop(k, None)
        _move_to_mru(ghost_S1, k)
        ghost_S0.pop(k, None)
    else:
        # If not tracked, assume it was probationary
        _move_to_mru(ghost_S0, k)
        ghost_S1.pop(k, None)

    # Cleanup timestamp for evicted key
    m_key_timestamp.pop(k, None)

    # Keep ghost history in check
    _trim_ghosts()
    # Rebalance after eviction
    _rebalance_segments()

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