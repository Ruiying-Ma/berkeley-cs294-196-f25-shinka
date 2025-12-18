# EVOLVE-BLOCK-START
"""Adaptive ARC-like cache eviction with SLRU segments, frequency aging and guard window"""

from collections import OrderedDict

# Segment structures
_T1_probation = OrderedDict()   # keys in cache, 1st-touch (recency-biased)
_T2_protected = OrderedDict()   # keys in cache, 2nd+ touch (frequency-biased)

# Ghost history (recently evicted keys) for adaptation
_B1_ghost = OrderedDict()       # evicted from T1
_B2_ghost = OrderedDict()       # evicted from T2

# Adaptive target size for probation (ARC's p). Float to allow smooth adjust.
_p_target = 0.0

# Estimated capacity (number of objects). Initialize lazily.
_cap_est = 0

# Fallback LRU timestamps if metadata desync occurs
m_key_timestamp = dict()

# Lightweight per-key frequency counter (hit count) with periodic aging
_freq = dict()   # key -> int
_last_age_tick = 0

# Admission guard based on protected evictions (time window)
_guard_until = 0
# Also track last victim strength (kept for continuity with earlier variants)
_last_victim_strength = 0.0
_VICTIM_GUARD_THRESH = 2.0  # threshold used when deriving guard time

# Tunable parameters
_P_INIT_RATIO = 0.33  # initial share for probation (T1)

def _ensure_capacity(cache_snapshot):
    """Initialize or update capacity estimate and clamp p."""
    global _cap_est, _p_target
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    if _cap_est <= 0:
        _cap_est = max(1, len(cache_snapshot.cache))
    # Initialize p if never set (zero and empty metadata)
    if _p_target == 0.0 and not _T1_probation and not _T2_protected and not _B1_ghost and not _B2_ghost:
        _p_target = max(0.0, min(float(_cap_est), float(_cap_est) * _P_INIT_RATIO))
    # Clamp p
    if _p_target < 0.0:
        _p_target = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)

def _get_targets(cache_snapshot):
    """Compute ARC targets from p: T1 target = round(p), T2 target = cap - T1 target."""
    _ensure_capacity(cache_snapshot)
    t1_target = int(round(_p_target))
    t2_target = max(_cap_est - t1_target, 0)
    return t1_target, t2_target

def _ghost_trim():
    """Limit ghost lists to capacity each (ARC-style bound)."""
    global _B1_ghost, _B2_ghost
    while len(_B1_ghost) > _cap_est:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > _cap_est:
        _B2_ghost.popitem(last=False)

def _maybe_age(cache_snapshot):
    """Periodically age frequencies to avoid stale bias."""
    global _last_age_tick
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    if now - _last_age_tick >= max(1, _cap_est):
        for k in list(_freq.keys()):
            newf = _freq.get(k, 0) // 2
            if newf <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = newf
        _last_age_tick = now

def _fallback_choose(cache_snapshot):
    """Fallback victim: global LRU by timestamp among cached keys."""
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    known = [(k, m_key_timestamp.get(k, None)) for k in keys]
    known_ts = [x for x in known if x[1] is not None]
    if known_ts:
        k = min(known_ts, key=lambda kv: kv[1])[0]
        return k
    return keys[0]

def _min_by_freq_ts(od, cache_snapshot):
    """Pick a victim by (frequency asc, timestamp asc) among keys present in cache from an OrderedDict."""
    best_k = None
    best_score = None
    for k in od.keys():
        if k not in cache_snapshot.cache:
            continue
        score = (_freq.get(k, 0), m_key_timestamp.get(k, 0))
        if best_score is None or score < best_score:
            best_score = score
            best_k = k
    return best_k

def _lru_key_in_odict(od, cache_snapshot):
    """Return the LRU key from OrderedDict that is currently in the cache."""
    for k in od.keys():
        if k in cache_snapshot.cache:
            return k
    return None

def _demote_protected_if_needed(cache_snapshot, avoid_key=None):
    """Ensure protected size does not exceed its ARC target by demoting LRU to T1."""
    _, t2_target = _get_targets(cache_snapshot)
    # Demote until within target
    while len(_T2_protected) > t2_target:
        lru = _lru_key_in_odict(_T2_protected, cache_snapshot)
        if lru is None:
            break
        # If LRU equals avoid_key (unlikely due to recency), pick next
        if avoid_key is not None and lru == avoid_key:
            # Move avoid_key to MRU to expose the next LRU
            _T2_protected.move_to_end(avoid_key, last=True)
            lru = _lru_key_in_odict(_T2_protected, cache_snapshot)
            if lru is None or lru == avoid_key:
                break
        _T2_protected.pop(lru, None)
        _T1_probation[lru] = True  # demoted reinserted as MRU in T1

def evict(cache_snapshot, obj):
    '''
    Choose victim key using adaptive ARC replace policy with frequency-aware selection.
    REPLACE(x): if |T1|>=1 and ((x in B2 and |T1| == p) or |T1| > p) -> evict from T1 else from T2.
    Within the chosen segment, pick the lowest-frequency (ties by older timestamp).
    '''
    _ensure_capacity(cache_snapshot)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    x_in_b2 = (obj is not None) and (obj.key in _B2_ghost)
    p_int = int(round(_p_target))
    choose_t1 = (t1_size >= 1) and ((x_in_b2 and t1_size == p_int) or (t1_size > _p_target))

    victim_key = None
    if choose_t1 and t1_size > 0:
        victim_key = _min_by_freq_ts(_T1_probation, cache_snapshot)
    if victim_key is None and t2_size > 0:
        victim_key = _min_by_freq_ts(_T2_protected, cache_snapshot)
    if victim_key is None and t1_size > 0:
        victim_key = _min_by_freq_ts(_T1_probation, cache_snapshot)
    if victim_key is None:
        victim_key = _fallback_choose(cache_snapshot)
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after cache hit.
    - If hit in probation (T1), promote to protected (T2).
    - If hit in protected, refresh recency.
    - Maintain timestamps and frequency with periodic aging.
    - Keep protected within ARC target via demotion of its LRU.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    # Update fallback LRU timestamp
    m_key_timestamp[key] = now
    # Increment frequency counter
    _freq[key] = _freq.get(key, 0) + 1

    if key in _T2_protected:
        # Refresh to MRU
        _T2_protected.move_to_end(key, last=True)
    elif key in _T1_probation:
        # Promote from probation to protected
        _T1_probation.pop(key, None)
        _T2_protected[key] = True  # insert as MRU
    else:
        # Metadata miss (hit without segment record): treat as frequent
        _T2_protected[key] = True

    # Respect ARC protected target by demoting its LRU if needed
    _demote_protected_if_needed(cache_snapshot, avoid_key=key)

    # Cleanup ghosts if any stale
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)
    _ghost_trim()

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata on insertion (miss path).
    - If the key is in ghost lists, adjust p (ARC adaptation) and insert into protected.
    - Otherwise insert into probation as MRU; if within guard window, place at LRU to resist scans.
    - Maintain timestamps, seed frequency minimally, and enforce protected target.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    if in_b1 or in_b2:
        # ARC adaptation of p (smooth float-based steps)
        global _p_target
        if in_b1:
            inc = max(1.0, float(len(_B2_ghost)) / max(1.0, float(len(_B1_ghost))))
            _p_target = min(float(_cap_est), _p_target + float(inc))
            _B1_ghost.pop(key, None)
        else:
            dec = max(1.0, float(len(_B1_ghost)) / max(1.0, float(len(_B2_ghost))))
            _p_target = max(0.0, _p_target - float(dec))
            _B2_ghost.pop(key, None)
        # Insert into protected (seen before)
        if key in _T1_probation:
            _T1_probation.pop(key, None)
        _T2_protected[key] = True
        # Seed frequency as at least 2 for re-referenced keys
        _freq[key] = max(_freq.get(key, 0) + 1, 2)

        # Keep protected within its target by demoting its LRU if necessary
        _demote_protected_if_needed(cache_snapshot, avoid_key=key)
    else:
        # New to cache and ghosts: insert into probation (T1)
        if key in _T2_protected:
            # Rare desync; ensure consistency (shouldn't happen on miss)
            _T2_protected.move_to_end(key, last=True)
        else:
            _T1_probation[key] = True
            # Admission guard: if we recently evicted strong protected, bias newcomer cold
            if now <= _guard_until or _last_victim_strength >= _VICTIM_GUARD_THRESH:
                _T1_probation.move_to_end(key, last=False)
        # Seed minimal frequency for new items
        _freq[key] = _freq.get(key, 0)

    # Avoid duplicates across structures
    if key in _T1_probation and key in _T2_protected:
        _T1_probation.pop(key, None)
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)
    _ghost_trim()

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction.
    - Remove victim from its resident segment.
    - Add to corresponding ghost list (B1 if from probation, B2 if from protected).
    - Track victim strength and open a short guard window if protected item with reuse was evicted.
    - Trim ghost lists and clean timestamps/frequency.
    '''
    _ensure_capacity(cache_snapshot)
    victim_key = evicted_obj.key

    was_t1 = victim_key in _T1_probation
    was_t2 = victim_key in _T2_protected

    # Track strength of the evicted item before removing counters
    fval = _freq.get(victim_key, 0)
    base_strength = float(fval)
    if was_t2:
        base_strength += 2.0  # extra credit for protected residency
    global _last_victim_strength, _guard_until
    _last_victim_strength = base_strength

    # Remove from resident segments and add to ghosts
    if was_t1:
        _T1_probation.pop(victim_key, None)
        _B1_ghost[victim_key] = True  # insert as MRU
    elif was_t2:
        _T2_protected.pop(victim_key, None)
        _B2_ghost[victim_key] = True  # insert as MRU
        # If we had to evict a strong protected item, enable a short guard window
        if fval >= 2:
            _guard_until = cache_snapshot.access_count + max(1, _cap_est // 2)
    else:
        # Unknown location; put in B1 by default
        _B1_ghost[victim_key] = True

    # Remove fallback timestamp and frequency for evicted key
    m_key_timestamp.pop(victim_key, None)
    _freq.pop(victim_key, None)

    _ghost_trim()
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