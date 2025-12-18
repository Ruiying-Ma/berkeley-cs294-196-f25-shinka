# EVOLVE-BLOCK-START
"""Adaptive ARC+SLRU with momentum, scan detection, and aged tiny-LFU assists."""

from collections import OrderedDict, deque
import math

# Fallback global LRU timestamps for safety
m_key_timestamp = dict()

# ARC structures
_T1_probation = OrderedDict()   # resident keys, recency-biased (first-touch)
_T2_protected = OrderedDict()   # resident keys, frequency-biased (second+ touch)
_B1_ghost = OrderedDict()       # evicted from T1: key -> timestamp
_B2_ghost = OrderedDict()       # evicted from T2: key -> timestamp

# Adaptive target for T1 size (ARC's p), with momentum
_p_target = 0.0
_p_momentum = 0.0

# Capacity estimate (number of objects)
_cap_est = 1

# Tiny-LFU-like counters (saturating small integers) and last access times
_freq = dict()          # key -> small int [0..7]
_last_access = dict()   # key -> last access timestamp
_LAST_AGE_AT = 0        # last time we aged

# Scan detection sliding window
_win_deque = deque()            # recent keys (up to W = 2*cap)
_win_counts = dict()            # key -> count within window
_win_hits = 0
_win_total = 0
_scan_until = 0                 # time until which scan mode remains active
_last_p_tune_access = 0

# Tunable parameters
_P_INIT_RATIO = 0.3
_FREQ_MAX = 7
_PROMOTE_DEFAULT = 1
_PROMOTE_SCAN = 2
_SCAN_UNIQUE_THRESH = 0.6
_SCAN_HITRATE_THRESH = 0.2


def _ensure_capacity(cache_snapshot):
    """Initialize cap and clamp _p_target within [0, cap]."""
    global _cap_est, _p_target
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    if _cap_est <= 0:
        _cap_est = max(1, len(cache_snapshot.cache))

    if _p_target == 0.0 and not _T1_probation and not _T2_protected and not _B1_ghost and not _B2_ghost:
        _p_target = float(max(0, int(_P_INIT_RATIO * _cap_est)))
    _clamp_p()


def _clamp_p():
    global _p_target
    if _p_target < 0.0:
        _p_target = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)


def _ghost_trim():
    """Keep each ghost list bounded by capacity."""
    while len(_B1_ghost) > _cap_est:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > _cap_est:
        _B2_ghost.popitem(last=False)


def _maybe_age_freq(now):
    """Periodically age frequency counters to avoid stale bias."""
    global _LAST_AGE_AT
    if now - _LAST_AGE_AT >= _cap_est:
        _LAST_AGE_AT = now
        for k in list(_freq.keys()):
            v = _freq.get(k, 0)
            if v <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = v // 2


def _win_size():
    return max(8, 2 * _cap_est)


def _window_record(key, is_hit, now):
    """Update sliding window stats and set/clear scan mode."""
    global _win_hits, _win_total, _scan_until

    # Update totals
    _win_total += 1
    if is_hit:
        _win_hits += 1

    # Maintain deque and counts
    _win_deque.append(key)
    _win_counts[key] = _win_counts.get(key, 0) + 1
    W = _win_size()
    while len(_win_deque) > W:
        old = _win_deque.popleft()
        cnt = _win_counts.get(old, 0)
        if cnt <= 1:
            _win_counts.pop(old, None)
        else:
            _win_counts[old] = cnt - 1

    # Derive simple scan indicators
    window_len = max(1, len(_win_deque))
    distinct_ratio = min(1.0, float(len(_win_counts)) / float(window_len))
    hit_rate = float(_win_hits) / float(max(1, _win_total))

    # Enter scan mode if conditions hold; extend duration by W
    if distinct_ratio > _SCAN_UNIQUE_THRESH and hit_rate < _SCAN_HITRATE_THRESH:
        _scan_until = max(_scan_until, now + W)


def _is_scan(now):
    return now < _scan_until


def _promote_threshold(now):
    return _PROMOTE_SCAN if _is_scan(now) else _PROMOTE_DEFAULT


def _adjust_p_with_momentum(sign, age, now):
    """Adjust _p_target on ghost hits with momentum and freshness weighting."""
    global _p_momentum, _p_target
    # sign: +1 for B1 (favor recency → increase p), -1 for B2 (favor frequency → decrease p)

    # Proportional step based on ghost list pressure (ARC-like)
    b1 = max(1, len(_B1_ghost))
    b2 = max(1, len(_B2_ghost))
    if sign > 0:
        ratio = b2 / b1
    else:
        ratio = b1 / b2
    base_step = min(max(1.0, ratio), 0.25 * _cap_est)

    # Freshness weight: fresher ghosts push more strongly
    cap_half = max(1, _cap_est // 2)
    w = 1.0 - (float(age) / float(cap_half))
    # clamp to [0.75, 1.5]
    if w < 0.75:
        w = 0.75
    if w > 1.5:
        w = 1.5

    step = base_step * w
    # Momentum to smooth oscillations
    _p_momentum = 0.5 * _p_momentum + (sign * step)
    _p_target += _p_momentum
    _clamp_p()


def _t1_size():
    return len(_T1_probation)


def _t2_size():
    return len(_T2_protected)


def _choose_lru(odict_obj, cache_snapshot):
    """Choose LRU key from an OrderedDict that is still in cache."""
    for k in odict_obj.keys():
        if k in cache_snapshot.cache:
            return k
    return None


def _fallback_choose(cache_snapshot):
    """Fallback victim by global oldest timestamp."""
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    k = min(keys, key=lambda x: m_key_timestamp.get(x, -1))
    return k


def evict(cache_snapshot, obj):
    '''
    Choose victim key using ARC-style policy with scan-aware bias.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count

    # Clean up metadata if any non-resident keys linger
    keys_in_cache = set(cache_snapshot.cache.keys())
    for k in list(_T1_probation.keys()):
        if k not in keys_in_cache:
            _T1_probation.pop(k, None)
    for k in list(_T2_protected.keys()):
        if k not in keys_in_cache:
            _T2_protected.pop(k, None)

    # Scan-mode: strongly prefer evicting from T1
    if _is_scan(now):
        victim = _choose_lru(_T1_probation, cache_snapshot)
        if victim is not None:
            return victim

    # ARC eviction choice
    if _t1_size() > max(1, int(_p_target)):
        victim = _choose_lru(_T1_probation, cache_snapshot)
        if victim is not None:
            return victim

    # Otherwise from T2
    victim = _choose_lru(_T2_protected, cache_snapshot)
    if victim is not None:
        return victim

    # If T2 empty or out of sync, try T1
    victim = _choose_lru(_T1_probation, cache_snapshot)
    if victim is not None:
        return victim

    # Fallback
    return _fallback_choose(cache_snapshot)


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after cache hit.
    - Promote from T1 to T2 based on threshold (2 touches in scan mode, else 1).
    - Refresh recency in resident segment.
    - Age tiny-LFU counters periodically.
    - Update sliding window and timestamps.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    # Sliding window and scan detection
    _window_record(key, is_hit=True, now=now)

    # Periodic frequency aging
    _maybe_age_freq(now)

    # Update timestamp and tiny-LFU counter (saturating)
    m_key_timestamp[key] = now
    _last_access[key] = now
    _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 1)

    # If in T2, refresh to MRU
    if key in _T2_protected:
        _T2_protected.move_to_end(key, last=True)
    elif key in _T1_probation:
        # Promote depending on threshold
        if _freq.get(key, 0) >= _promote_threshold(now):
            # Move to protected MRU
            _T1_probation.pop(key, None)
            _T2_protected[key] = True
        else:
            # Stay in T1, refresh MRU
            _T1_probation.move_to_end(key, last=True)
    else:
        # Metadata desync: treat as strong, place in protected
        _T2_protected[key] = True

    # Touching a key invalidates stale ghosts (if present)
    _B1_ghost.pop(key, None)
    _B2_ghost.pop(key, None)
    _ghost_trim()

    # Under scan mode, gradually lower p_target every 100 accesses
    global _last_p_tune_access
    if _is_scan(now) and now - _last_p_tune_access >= 100:
        dec = 1.5 * max(1.0, len(_B1_ghost) / max(1.0, len(_B2_ghost) if len(_B2_ghost) > 0 else 1.0))
        _p_target = max(0.0, _p_target - dec)
        _last_p_tune_access = now


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after inserting a new object (miss).
    - ARC ghost feedback with momentum and freshness weighting adjusts p_target.
    - Re-admission: if ghost was fresh, insert directly into T2 with boosted frequency,
      otherwise start in T1.
    - Scan-sensitive: require two touches to promote; never direct-protect newcomers unless
      strong ghost evidence exists.
    '''
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    key = obj.key

    # Sliding window and scan detection
    _window_record(key, is_hit=False, now=now)

    # Periodic frequency aging
    _maybe_age_freq(now)

    # Admission via ghosts
    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost
    placed_in_protected = False

    if in_b1 or in_b2:
        # Freshness
        ghost_time = (_B1_ghost.get(key) if in_b1 else _B2_ghost.get(key)) or now
        age = now - ghost_time
        # Momentum-based p adjustment
        _adjust_p_with_momentum(+1 if in_b1 else -1, age=age, now=now)

        # Fresh ghost => direct to T2, else T1
        if age <= max(1, _cap_est // 2):
            # Directly protect; seed frequency higher based on origin
            _T2_protected[key] = True
            _T1_probation.pop(key, None)
            _freq[key] = min(_FREQ_MAX, 3 if in_b2 else 2)
            placed_in_protected = True
        else:
            # Stale ghost: start in T1
            _T1_probation[key] = True
            _T2_protected.pop(key, None)
            _freq[key] = 0

        # Remove from the corresponding ghost list
        if in_b1:
            _B1_ghost.pop(key, None)
        else:
            _B2_ghost.pop(key, None)
    else:
        # New key: start in probation. In scan mode, strictly avoid direct protection.
        _T1_probation[key] = True
        _T2_protected.pop(key, None)
        _freq[key] = 0

    # Update timestamps
    _last_access[key] = now
    m_key_timestamp[key] = now

    # Bound ghost lists
    _ghost_trim()

    # Under scan mode, gradually lower p_target every 100 accesses
    global _last_p_tune_access
    if _is_scan(now) and now - _last_p_tune_access >= 100:
        dec = 1.5 * max(1.0, len(_B1_ghost) / max(1.0, len(_B2_ghost) if len(_B2_ghost) > 0 else 1.0))
        _p_target = max(0.0, _p_target - dec)
        _last_p_tune_access = now


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction.
    - Remove victim from resident segment.
    - Place into corresponding ghost list with timestamp.
    - Clean per-key state for evicted key to keep structures small.
    '''
    _ensure_capacity(cache_snapshot)
    if evicted_obj is None:
        return
    now = cache_snapshot.access_count
    victim_key = evicted_obj.key

    was_t1 = victim_key in _T1_probation
    was_t2 = victim_key in _T2_protected

    # Remove from live segments
    _T1_probation.pop(victim_key, None)
    _T2_protected.pop(victim_key, None)

    # Add to appropriate ghost list as MRU with timestamp
    if was_t2:
        _B2_ghost[victim_key] = now
    else:
        _B1_ghost[victim_key] = now

    # Clean auxiliary metadata
    _freq.pop(victim_key, None)
    _last_access.pop(victim_key, None)
    m_key_timestamp.pop(victim_key, None)

    # Trim ghosts
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