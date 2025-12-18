# EVOLVE-BLOCK-START
"""Adaptive SLRU+LRFU eviction with TinyLFU-biased victim selection

- SLRU segments: probation (recency) and protected (frequency)
- LRFU: exponentially decayed score per key (recency + frequency)
- TinyLFU: CM-sketch with aging for admission, promotion, and victim bias
- Dynamic decay half-life for the LRFU score based on workload (miss streak)
"""

# Base configuration
DECAY_HALF_LIFE = 16  # default half-life (in accesses) for decayed score

# Per-key metadata for cached objects
_key_score = dict()      # key -> float decayed frequency score
_key_last_time = dict()  # key -> int last access_count when we updated its score

# Segmented resident sets
_probation = set()       # keys admitted recently or with single hit
_protected = set()       # keys with demonstrated frequency
_prot_cap = 0            # adaptive target size (in number of keys) for protected segment

# TinyLFU-style frequency sketch (small CM-sketch with periodic aging)
_sketch_d = 4
_sketch_width_power = 12  # width = 4096 for better accuracy
_sketch_w = 1 << _sketch_width_power
_sketch_mask = _sketch_w - 1
_sketch_tables = [[0] * _sketch_w for _ in range(_sketch_d)]
_sketch_ops = 0
_sketch_age_period = max(2048, _sketch_w)
_sketch_seeds = (0x9e3779b1, 0x85ebca77, 0xc2b2ae3d, 0x27d4eb2f)

# Simple scan/hotness detectors
_miss_streak = 0
_decay_hl = DECAY_HALF_LIFE  # dynamic half-life used for decayed scores

def _decay_base():
    """Compute dynamic decay base from the current half-life."""
    hl = max(4, min(64, _decay_hl))
    return 2 ** (-1.0 / hl)

def _sketch_idx(h: int, i: int) -> int:
    s = _sketch_seeds[i % len(_sketch_seeds)]
    x = (h ^ s) * 0x9e3779b97f4a7c15
    return (x ^ (x >> 33)) & _sketch_mask

def _sketch_maybe_age():
    global _sketch_ops
    _sketch_ops += 1
    if _sketch_ops % _sketch_age_period == 0:
        for t in _sketch_tables:
            for j in range(_sketch_w):
                t[j] >>= 1

def _sketch_increment(key: str, amount: int = 1):
    h = hash(key)
    for i in range(_sketch_d):
        idx = _sketch_idx(h, i)
        v = _sketch_tables[i][idx] + amount
        _sketch_tables[i][idx] = 255 if v > 255 else v
    _sketch_maybe_age()

def _sketch_estimate(key: str) -> int:
    h = hash(key)
    est = 1 << 30
    for i in range(_sketch_d):
        v = _sketch_tables[i][_sketch_idx(h, i)]
        if v < est:
            est = v
    return est

def _prune_membership(cache_snapshot):
    """Remove keys from segment sets that are no longer resident in the cache."""
    cache_keys = set(cache_snapshot.cache.keys())
    for seg in (_probation, _protected):
        stale = [k for k in seg if k not in cache_keys]
        for k in stale:
            seg.discard(k)

def _ensure_meta(k, now):
    """Ensure metadata exists for key k and lazily decay its score to 'now'."""
    if k not in _key_last_time:
        _key_last_time[k] = now
    if k not in _key_score:
        _key_score[k] = 0.0
    dt = now - _key_last_time[k]
    if dt > 0:
        _key_score[k] *= pow(_decay_base(), dt)
        _key_last_time[k] = now
    return _key_score[k], _key_last_time[k]

def _pick_min_by_score(candidates, now, bias_freq=None):
    """
    Pick key with minimal adjusted decayed score.
    - If bias_freq is provided (TinyLFU estimate of the incoming key), add a penalty
      to candidates whose TinyLFU estimate > bias_freq to avoid evicting hotter keys.
    Tie-breakers: lower TinyLFU estimate first, then older last access.
    """
    min_key = None
    min_adj = None
    min_est = None
    min_old_time = None
    for k in candidates:
        old_time = _key_last_time.get(k, now)
        s, _ = _ensure_meta(k, now)
        fv = _sketch_estimate(k)
        adj = s
        if bias_freq is not None and fv > bias_freq:
            # Penalize victims with higher frequency than the incoming object.
            # The factor moderates the impact to still allow eviction if necessary.
            adj += 0.25 * (fv - bias_freq)
        if (min_adj is None) or (adj < min_adj) or \
           (adj == min_adj and (min_est is None or fv < min_est)) or \
           (adj == min_adj and fv == min_est and old_time < (min_old_time if min_old_time is not None else old_time)):
            min_adj = adj
            min_key = k
            min_est = fv
            min_old_time = old_time
    return min_key

def _enforce_protected_cap(now):
    """If protected exceeds target cap, demote lowest-score protected key to probation."""
    global _prot_cap
    while _protected and len(_protected) > _prot_cap:
        k = _pick_min_by_score(_protected, now)
        if k is None:
            break
        _protected.discard(k)
        _probation.add(k)

def evict(cache_snapshot, obj):
    '''
    Choose eviction victim using SLRU segments, LRFU scores, and TinyLFU-biased selection.
    Prefer evicting from probation. If evicting from protected, avoid evicting keys whose
    TinyLFU frequency exceeds that of the incoming object.
    '''
    global _prot_cap
    now = cache_snapshot.access_count

    _prune_membership(cache_snapshot)

    # Initialize protected capacity target if unset
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    f_new = _sketch_estimate(obj.key)

    # Prefer evicting from probation
    prob_candidates = [k for k in _probation if k in cache_snapshot.cache]
    if prob_candidates:
        return _pick_min_by_score(prob_candidates, now)

    # From protected: bias against evicting hotter-than-incoming items
    prot_candidates = [k for k in _protected if k in cache_snapshot.cache]
    if prot_candidates:
        # Try biased selection first
        victim = _pick_min_by_score(prot_candidates, now, bias_freq=f_new)
        if victim is not None:
            return victim

    # Fallback: choose globally minimal adjusted score among resident keys
    return _pick_min_by_score(list(cache_snapshot.cache.keys()), now, bias_freq=f_new)

def update_after_hit(cache_snapshot, obj):
    '''
    Update policy state after a cache hit on obj.
    - Increment TinyLFU sketch
    - Update decayed LRFU score
    - Promotion from probation if qualified
    - Adapt protected cap; slow down decay slightly during hit-heavy periods
    '''
    global _prot_cap, _miss_streak, _decay_hl
    now = cache_snapshot.access_count

    _prune_membership(cache_snapshot)

    # Initialize protected capacity target if unset
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    # Count this access in the frequency sketch
    _sketch_increment(obj.key, 1)

    # Update decayed score and last time
    _ensure_meta(obj.key, now)

    # Reset miss streak on hit and relax decay a bit (remember more)
    _miss_streak = 0
    _decay_hl = min(48, _decay_hl + 2)

    # Promotion logic gated by TinyLFU estimate
    est = _sketch_estimate(obj.key)
    if obj.key in _probation:
        # Promote when frequency suggests future utility
        if est >= 2:
            _probation.discard(obj.key)
            _protected.add(obj.key)
            if _prot_cap < cur_cap:
                _prot_cap += 1
    elif obj.key in _protected:
        # Already protected
        pass
    else:
        # Cache hit but unknown to segments: place appropriately based on estimate
        if est >= 2:
            _protected.add(obj.key)
            _probation.discard(obj.key)
        else:
            _probation.add(obj.key)
            _protected.discard(obj.key)

    # Frequency boost; slightly stronger in protected
    if obj.key in _protected:
        _key_score[obj.key] += 1.5
    else:
        _key_score[obj.key] += 1.0

    # Enforce protected capacity by demoting weakest protected key
    _enforce_protected_cap(now)

def update_after_insert(cache_snapshot, obj):
    '''
    Update policy state immediately after inserting a new object (a miss admission).
    - Increment TinyLFU for admission accounting
    - Dynamic admission: hot-admit to protected when estimate is high, else probation
    - Adjust protected target under scans; speed up decay under sustained misses
    '''
    global _prot_cap, _miss_streak, _decay_hl
    now = cache_snapshot.access_count

    _prune_membership(cache_snapshot)

    # Initialize protected capacity on cold start
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap <= 0:
        _prot_cap = max(1, cur_cap // 2)

    # Count this access (miss) in the frequency sketch
    _sketch_increment(obj.key, 1)
    est = _sketch_estimate(obj.key)

    # Dynamic admission threshold: tougher when protected is at/over target
    admit_thr = 2 + (1 if len(_protected) >= _prot_cap else 0)

    if est >= admit_thr:
        _protected.add(obj.key)
        _probation.discard(obj.key)
        _key_last_time[obj.key] = now
        _key_score[obj.key] = 0.7  # slightly higher initial score for protected admits
        if _prot_cap < cur_cap:
            _prot_cap += 1
    else:
        _probation.add(obj.key)
        _protected.discard(obj.key)
        _key_last_time[obj.key] = now
        _key_score[obj.key] = 0.1  # conservative initial score to avoid scan pollution

    # Update miss streak and apply scan guard + faster forgetting when scans suspected
    _miss_streak += 1
    if _miss_streak > 2 * cur_cap:
        # Shift toward recency under sustained misses (scans)
        target_min = max(1, cur_cap // 4)
        if _prot_cap > target_min:
            _prot_cap = max(target_min, _prot_cap - max(1, _prot_cap // 2))
        # Accelerate decay to forget stale history faster
        _decay_hl = max(6, _decay_hl // 2)
    else:
        # Gradually move decay back toward default
        if _decay_hl < DECAY_HALF_LIFE:
            _decay_hl += 1
        elif _decay_hl > DECAY_HALF_LIFE:
            _decay_hl -= 1

    # Enforce protected capacity by demoting weakest protected key if needed
    _enforce_protected_cap(now)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update policy state after evicting the victim.
    - Clear per-key metadata and segment membership for the victim
    - Keep protected cap within current resident size
    '''
    global _prot_cap
    _key_score.pop(evicted_obj.key, None)
    _key_last_time.pop(evicted_obj.key, None)
    _probation.discard(evicted_obj.key)
    _protected.discard(evicted_obj.key)

    # Keep protected cap bounded by current possible size
    cur_cap = max(1, len(cache_snapshot.cache))
    if _prot_cap > cur_cap:
        _prot_cap = cur_cap

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