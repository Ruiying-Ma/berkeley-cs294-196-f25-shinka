# EVOLVE-BLOCK-START
"""Adaptive ARC-like cache with scan detection, momentum p-adjust, and tiny SLFU counters"""

from collections import OrderedDict, deque

# Segments (in-cache)
_T1_probation = OrderedDict()   # 1st-touch, recency biased (LRU -> MRU in order)
_T2_protected = OrderedDict()   # 2nd+ touch, frequency biased

# Ghost history (metadata only, not in cache). Store last-evict timestamps for freshness.
_B1_ghost = OrderedDict()       # from T1: key -> last_evicted_ts
_B2_ghost = OrderedDict()       # from T2: key -> last_evicted_ts

# Adaptive target size for T1 (ARC's p)
_p_target = 0.0
_p_momentum = 0.0

# Estimated capacity (number of objects). Initialize lazily.
_cap_est = 0

# Fallback timestamps for LRU choice if metadata desync occurs
m_key_timestamp = dict()

# Tiny saturating frequency with aging (SLFU)
_freq = dict()                  # key -> small int [0..7]
_FREQ_MAX = 7
_last_freq_aging_at = 0         # access_count of last global aging

# Sliding window for scan detection and recent promotions
_win_size = 0
_win_hits = deque()             # 1 for hit, 0 for miss
_win_promotions = deque()       # 1 when T1->T2 promotion occurred, else 0
_win_keys = deque()             # keys for unique-rate estimation
_win_key_counts = dict()        # key -> count in window
_unique_in_window = 0
_scan_mode_until = 0
_last_scan_adjust_at = 0

# Tunable parameters
_P_INIT_RATIO = 0.3
_SCAN_HIT_THRESH = 0.2
_SCAN_UNIQUE_THRESH = 0.6
_T2_SAMPLE_BASE = 3
_T2_SAMPLE_BOOST = 5


def _ensure_capacity(cache_snapshot):
    """Initialize/refresh capacity estimate, window size, and clamp p."""
    global _cap_est, _p_target, _win_size
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    if _cap_est <= 0:
        _cap_est = max(1, len(cache_snapshot.cache))
    # Initialize p once at cold start
    if _p_target == 0.0 and not _T1_probation and not _T2_protected and not _B1_ghost and not _B2_ghost:
        _p_target = max(0.0, min(float(_cap_est), float(_cap_est) * _P_INIT_RATIO))
    # Clamp p
    if _p_target < 0.0:
        _p_target = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
    # Window size based on capacity
    if _win_size == 0:
        _win_size = max(32, min(4096, 2 * _cap_est))


def _ghost_trim():
    """Limit ghost lists to capacity each (ARC-style bound)."""
    global _B1_ghost, _B2_ghost
    while len(_B1_ghost) > _cap_est:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > _cap_est:
        _B2_ghost.popitem(last=False)


def _fallback_choose(cache_snapshot):
    """Fallback victim: global LRU by timestamp among cached keys."""
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    known = [(k, m_key_timestamp.get(k, None)) for k in keys]
    known_ts = [x for x in known if x[1] is not None]
    if known_ts:
        return min(known_ts, key=lambda kv: kv[1])[0]
    return keys[0]


def _record_access(cache_snapshot, key, was_hit, was_promotion):
    """Update sliding-window stats for scan detection and promotions, and unique-rate."""
    global _unique_in_window
    # Hits
    _win_hits.append(1 if was_hit else 0)
    if len(_win_hits) > _win_size:
        _win_hits.popleft()
    # Promotions
    _win_promotions.append(1 if was_promotion else 0)
    if len(_win_promotions) > _win_size:
        _win_promotions.popleft()
    # Unique rate tracking
    _win_keys.append(key)
    prev = _win_key_counts.get(key, 0)
    _win_key_counts[key] = prev + 1
    if prev == 0:
        _unique_in_window += 1
    if len(_win_keys) > _win_size:
        old = _win_keys.popleft()
        cnt = _win_key_counts.get(old, 0)
        if cnt <= 1:
            _win_key_counts.pop(old, None)
            _unique_in_window -= 1
        else:
            _win_key_counts[old] = cnt - 1


def _in_scan_mode(cache_snapshot):
    return cache_snapshot.access_count < _scan_mode_until


def _maybe_update_scan_mode(cache_snapshot):
    """Enter or exit scan mode based on window stats."""
    global _scan_mode_until
    total = len(_win_hits)
    if total < max(16, _win_size // 2):
        return
    hit_rate = sum(_win_hits) / float(total) if total > 0 else 0.0
    unique_rate = (_unique_in_window / float(len(_win_keys))) if _win_keys else 0.0
    now = cache_snapshot.access_count
    if (unique_rate > _SCAN_UNIQUE_THRESH) and (hit_rate < _SCAN_HIT_THRESH):
        _scan_mode_until = max(_scan_mode_until, now + _win_size)


def _freq_bump(key):
    """Increment small saturating frequency."""
    v = _freq.get(key, 0)
    if v < _FREQ_MAX:
        _freq[key] = v + 1


def _freq_age(cache_snapshot):
    """Periodically age frequencies to avoid stale bias."""
    global _last_freq_aging_at
    now = cache_snapshot.access_count
    if now - _last_freq_aging_at >= max(64, _cap_est):
        for k in list(_freq.keys()):
            nv = _freq[k] // 2
            if nv <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = nv
        _last_freq_aging_at = now


def _adjust_p_with_momentum(sign, this_ghost_len, other_ghost_len, ghost_age, now):
    """Smooth p updates using momentum and ghost freshness weighting."""
    global _p_target, _p_momentum
    # Base step depends on imbalance; cap by 0.25*cap
    ratio = (other_ghost_len / max(1.0, float(this_ghost_len)))
    step = max(1.0, ratio)
    step = min(step, 0.25 * float(_cap_est))
    # Freshness weighting
    cap_half = max(1.0, float(_cap_est) / 2.0)
    w = 1.0 - float(ghost_age) / cap_half
    if w < 0.75:
        w = 0.75
    if w > 1.5:
        w = 1.5
    # Momentum update
    _p_momentum = 0.5 * _p_momentum + float(sign) * step * w
    _p_target += _p_momentum
    if _p_target < 0.0:
        _p_target = 0.0
        _p_momentum = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
        _p_momentum = 0.0


def _t2_sample_size():
    """Adaptive T2 sampling based on pressure and recent promotions."""
    protected = len(_T2_protected)
    target_t1 = int(round(_p_target))
    # When protected is crowded (protected > cap - target_t1) and promotions are frequent, boost sampling
    crowded = protected > max(0, _cap_est - target_t1)
    promo_rate = (sum(_win_promotions) / float(len(_win_promotions))) if _win_promotions else 0.0
    if crowded and promo_rate > 0.2:
        return _T2_SAMPLE_BOOST
    return _T2_SAMPLE_BASE


def evict(cache_snapshot, obj):
    '''
    Choose victim key using adaptive ARC-like policy with T2 sampling and scan resistance.
    Prefer evicting from T1 when it exceeds target p; otherwise evict from T2.
    '''
    _ensure_capacity(cache_snapshot)

    # Keep metadata consistent with actual cache content (lightweight check)
    for d in (_T1_probation, _T2_protected):
        for k in list(d.keys()):
            if k not in cache_snapshot.cache:
                d.pop(k, None)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    now = cache_snapshot.access_count

    # Scan bias: prefer evicting from T1 during scans
    from_t1 = False
    if t1_size > 0:
        if _in_scan_mode(cache_snapshot):
            from_t1 = True
        else:
            from_t1 = (t1_size > max(0, int(_p_target))) or (t2_size == 0)

    if from_t1 and t1_size > 0:
        # LRU from T1
        victim_key = next(iter(_T1_probation.keys()))
        return victim_key

    if t2_size > 0:
        # Adaptive sample among oldest in T2 using tiny frequency, tie-break by recency
        sample_n = min(_t2_sample_size(), t2_size)
        # Gather oldest 'sample_n' keys
        it = iter(_T2_protected.keys())
        candidates = []
        for _ in range(sample_n):
            try:
                candidates.append(next(it))
            except StopIteration:
                break
        # Choose minimal by (freq, fallback timestamp)
        def t2_score(k):
            return (_freq.get(k, 0), m_key_timestamp.get(k, now))
        victim_key = min(candidates, key=t2_score)
        return victim_key

    # If T2 empty but T1 has items
    if t1_size > 0:
        return next(iter(_T1_probation.keys()))

    # Fallback
    return _fallback_choose(cache_snapshot)


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after cache hit.
    - If hit in probation (T1), promote to protected (T2) unless in scan mode and not yet double-touched.
    - If hit in protected, refresh recency.
    - Maintain fallback timestamp map and scan stats.
    '''
    _ensure_capacity(cache_snapshot)
    _freq_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count

    # Update fallback LRU timestamp and frequency
    m_key_timestamp[key] = now
    _freq_bump(key)

    was_promotion = False
    if key in _T2_protected:
        # Refresh to MRU
        _T2_protected.move_to_end(key, last=True)
    elif key in _T1_probation:
        # In scan mode, require two touches to promote
        if _in_scan_mode(cache_snapshot) and _freq.get(key, 0) < 2:
            # Refresh within T1
            _T1_probation.move_to_end(key, last=True)
        else:
            # Promote from T1 to T2
            _T1_probation.pop(key, None)
            _T2_protected[key] = True
            was_promotion = True
    else:
        # Metadata miss: cache has it but we don't; add to T2 to avoid premature eviction
        _T2_protected[key] = True

    # Adjust scan mode state and record
    _record_access(cache_snapshot, key, was_hit=True, was_promotion=was_promotion)
    _maybe_update_scan_mode(cache_snapshot)

    # In scan mode, gradually tilt p downward periodically to resist pollution
    global _last_scan_adjust_at, _p_target
    if _in_scan_mode(cache_snapshot) and now - _last_scan_adjust_at >= max(32, _cap_est // 2):
        # Decrease p modestly with bias from ghost imbalance
        b1 = len(_B1_ghost)
        b2 = len(_B2_ghost)
        other_over_this = (b2 / max(1.0, b1)) if b1 > 0 else 1.0
        step = min(max(1.0, other_over_this), 0.25 * float(_cap_est))
        _p_target = max(0.0, _p_target - step)
        _last_scan_adjust_at = now

    _ghost_trim()


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata on insertion (cache miss path).
    - If key is in ghosts, adjust p with momentum and place MRU into T2 if fresh; else T1.
    - Otherwise insert into T1 MRU.
    - Maintain fallback timestamp map and scan stats.
    '''
    _ensure_capacity(cache_snapshot)
    _freq_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    placed_in_t2 = False

    if in_b1 or in_b2:
        # Momentum p-adjust with freshness
        if in_b1:
            last = _B1_ghost.get(key, now)
            age = now - last
            _adjust_p_with_momentum(+1.0, len(_B1_ghost), len(_B2_ghost), age, now)
            _B1_ghost.pop(key, None)
        else:
            last = _B2_ghost.get(key, now)
            age = now - last
            _adjust_p_with_momentum(-1.0, len(_B2_ghost), len(_B1_ghost), age, now)
            _B2_ghost.pop(key, None)

        # Admission: fresh ghosts go directly to T2, stale ghosts go to T1
        if age <= max(1, _cap_est // 2) and not _in_scan_mode(cache_snapshot):
            _T2_protected[key] = True  # MRU in T2
            placed_in_t2 = True
            # Seed frequency stronger for B2 origin
            if in_b2:
                _freq[key] = max(_freq.get(key, 0), 3)
            else:
                _freq[key] = max(_freq.get(key, 0), 2)
        else:
            # Stale ghost or in scan mode: insert into T1
            _T1_probation[key] = True
            _freq.setdefault(key, 0)
    else:
        # New to the system: insert into T1 MRU
        _T1_probation[key] = True
        _freq.setdefault(key, 0)

    # Keep structures consistent (avoid duplicates)
    if placed_in_t2 and key in _T1_probation:
        _T1_probation.pop(key, None)
    if not placed_in_t2 and key in _T2_protected:
        _T2_protected.pop(key, None)

    # Record stats
    _record_access(cache_snapshot, key, was_hit=False, was_promotion=False)
    _maybe_update_scan_mode(cache_snapshot)
    _ghost_trim()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction.
    - Remove victim from its resident segment.
    - Add to corresponding ghost list (B1 if from T1, B2 if from T2) with timestamp.
    - Trim ghost lists to capacity and clean timestamps/freq.
    '''
    _ensure_capacity(cache_snapshot)
    victim_key = evicted_obj.key
    now = cache_snapshot.access_count

    was_t1 = victim_key in _T1_probation
    was_t2 = victim_key in _T2_protected

    # Remove from resident segments
    if was_t1:
        _T1_probation.pop(victim_key, None)
        _B1_ghost[victim_key] = now
    elif was_t2:
        _T2_protected.pop(victim_key, None)
        _B2_ghost[victim_key] = now
    else:
        # Unknown location; default to B1
        _B1_ghost[victim_key] = now

    # Clean up fallback timestamp and frequency
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