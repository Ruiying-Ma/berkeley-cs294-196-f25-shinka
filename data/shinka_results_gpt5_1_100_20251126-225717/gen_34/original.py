# EVOLVE-BLOCK-START
"""Adaptive ARC-like cache eviction with SLRU segments, SLFU sampling, and scan detection"""

from collections import OrderedDict, deque

# Segment structures
_T1_probation = OrderedDict()   # keys in cache, 1st-touch (recency-biased) oldest->newest
_T2_protected = OrderedDict()   # keys in cache, 2nd+ touch (frequency-biased) oldest->newest

# Ghost history (recently evicted keys) for adaptation; store last-evict timestamp
_B1_ghost = OrderedDict()       # evicted from T1: key -> last_evicted_ts
_B2_ghost = OrderedDict()       # evicted from T2: key -> last_evicted_ts

# Adaptive target size for probation (ARC's p). Float to allow smooth adjust.
_p_target = 0.0

# Estimated capacity (number of objects). Initialize lazily.
_cap_est = 0

# Fallback LRU timestamps if metadata desync occurs
m_key_timestamp = dict()

# Tiny saturating LFU counters with periodic aging (SLFU)
_freq = dict()                  # key -> small int [0..7]
_FREQ_MAX = 7
_last_freq_aging_at = 0

# Sliding window for scan detection
_win_size = 0
_win_hits = deque()             # 1 for hit, 0 for miss
_win_keys = deque()             # recent keys for unique-rate
_win_key_counts = dict()        # key -> count in window
_unique_in_window = 0
_scan_mode_until = 0
_last_scan_adjust_at = 0

# Tunable parameters
_P_INIT_RATIO = 0.3  # initial share for probation (T1)
_SCAN_HIT_THRESH = 0.2
_SCAN_UNIQUE_THRESH = 0.6

def _ensure_capacity(cache_snapshot):
    """Initialize or update capacity estimate and clamp p."""
    global _cap_est, _p_target, _win_size
    cap = getattr(cache_snapshot, "capacity", None)
    # Some runners define capacity as number of objects; if absent, infer
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
    # Window size based on capacity
    if _win_size == 0:
        _win_size = max(32, min(4096, 2 * _cap_est))

def _ghost_trim():
    """Limit ghost lists to capacity each (ARC-style bound)."""
    global _B1_ghost, _B2_ghost
    # Trim oldest entries beyond capacity
    while len(_B1_ghost) > _cap_est:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > _cap_est:
        _B2_ghost.popitem(last=False)

def _fallback_choose(cache_snapshot):
    """Fallback victim: global LRU by timestamp among cached keys."""
    # Prefer the minimum timestamp; if unknown, pick arbitrary
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    # Filter to known timestamps
    known = [(k, m_key_timestamp.get(k, None)) for k in keys]
    known_ts = [x for x in known if x[1] is not None]
    if known_ts:
        k = min(known_ts, key=lambda kv: kv[1])[0]
        return k
    return keys[0]

def _freq_bump(key):
    v = _freq.get(key, 0)
    if v < _FREQ_MAX:
        _freq[key] = v + 1

def _freq_age(cache_snapshot):
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

def _record_access(cache_snapshot, key, was_hit):
    """Update sliding-window stats for scan detection and unique-rate."""
    global _unique_in_window
    _win_hits.append(1 if was_hit else 0)
    if len(_win_hits) > _win_size:
        _win_hits.popleft()
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

def _maybe_update_scan_mode(cache_snapshot):
    """Enter scan mode if unique rate is high and hit rate is low."""
    global _scan_mode_until
    total = len(_win_hits)
    if total < max(16, _win_size // 2):
        return
    hit_rate = sum(_win_hits) / float(total) if total > 0 else 0.0
    unique_rate = (_unique_in_window / float(len(_win_keys))) if _win_keys else 0.0
    now = cache_snapshot.access_count
    if (unique_rate > _SCAN_UNIQUE_THRESH) and (hit_rate < _SCAN_HIT_THRESH):
        _scan_mode_until = max(_scan_mode_until, now + _win_size)

def _in_scan_mode(cache_snapshot):
    return cache_snapshot.access_count < _scan_mode_until

def _t2_sample_size():
    """Adaptive sampling size for T2 victim selection."""
    target_t1 = int(max(0, round(_p_target)))
    crowded = len(_T2_protected) > max(0, _cap_est - target_t1)
    return 5 if crowded else 3

def evict(cache_snapshot, obj):
    '''
    Choose victim key using adaptive ARC-like policy with scan bias and SLFU sampling.
    Prefer evicting from probation (T1) when it exceeds target p or during scans;
    otherwise sample from protected (T2) by lowest tiny frequency among oldest entries.
    '''
    _ensure_capacity(cache_snapshot)
    _freq_age(cache_snapshot)

    # Keep segment metadata consistent with actual cache content
    for d in (_T1_probation, _T2_protected):
        for k in list(d.keys()):
            if k not in cache_snapshot.cache:
                d.pop(k, None)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    now = cache_snapshot.access_count

    choose_t1 = False
    if t1_size > 0:
        choose_t1 = _in_scan_mode(cache_snapshot) or (t1_size > max(1, int(_p_target))) or (t2_size == 0)

    if choose_t1 and t1_size > 0:
        # LRU from T1
        return next(iter(_T1_probation.keys()))

    if t2_size > 0:
        # Sample among oldest T2 candidates by tiny frequency; tie-break by recency
        sample_n = min(_t2_sample_size(), t2_size)
        it = iter(_T2_protected.keys())
        candidates = []
        for _ in range(sample_n):
            try:
                candidates.append(next(it))
            except StopIteration:
                break
        def t2_score(k):
            return (_freq.get(k, 0), m_key_timestamp.get(k, now))
        return min(candidates, key=t2_score)

    # If T2 empty but T1 has items
    if t1_size > 0:
        return next(iter(_T1_probation.keys()))

    # Fallback to global LRU if metadata desync
    return _fallback_choose(cache_snapshot)

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after cache hit.
    - If hit in probation (T1), promote to protected (T2) unless scan mode requires two touches.
    - If hit in protected, refresh recency.
    - Maintain fallback timestamp map and scan stats.
    '''
    _ensure_capacity(cache_snapshot)
    _freq_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    # Update fallback LRU timestamp and tiny frequency
    m_key_timestamp[key] = now
    _freq_bump(key)

    was_hit = True

    if key in _T2_protected:
        # Refresh to MRU
        _T2_protected.move_to_end(key, last=True)
    elif key in _T1_probation:
        # In scan mode, require two touches before promotion
        if _in_scan_mode(cache_snapshot) and _freq.get(key, 0) < 2:
            _T1_probation.move_to_end(key, last=True)
        else:
            _T1_probation.pop(key, None)
            _T2_protected[key] = True  # insert as MRU
    else:
        # Metadata miss: cache has it but we don't; treat as frequent and add to protected
        _T2_protected[key] = True

    # Touch ghosts cleanup if any stale
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)

    # Sliding window tracking and scan mode upkeep
    _record_access(cache_snapshot, key, was_hit=was_hit)
    _maybe_update_scan_mode(cache_snapshot)

    # In scan mode, gradually tilt p downward to resist pollution
    global _last_scan_adjust_at, _p_target
    if _in_scan_mode(cache_snapshot) and now - _last_scan_adjust_at >= max(32, _cap_est // 2):
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
    - If the key is in ghost lists, adjust p (ARC adaptation) and insert into protected only if ghost is fresh.
    - Otherwise insert into probation as MRU.
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
        # ARC adaptation of p
        global _p_target
        if in_b1:
            # Favor recency: increase p
            inc = max(1, len(_B2_ghost) // max(1, len(_B1_ghost)))
            _p_target = min(float(_cap_est), _p_target + float(inc))
            last = _B1_ghost.get(key, None)
            _B1_ghost.pop(key, None)
        else:
            # Favor frequency: decrease p
            dec = max(1, len(_B1_ghost) // max(1, len(_B2_ghost)))
            _p_target = max(0.0, _p_target - float(dec))
            last = _B2_ghost.get(key, None)
            _B2_ghost.pop(key, None)

        # Admission: only place directly into T2 if ghost is fresh and not in scan mode
        # Handle legacy True values by treating them as stale
        age = None
        if isinstance(last, int):
            age = now - last
        else:
            age = _cap_est * 10  # treat as stale if no timestamp
        if (age <= max(1, _cap_est // 2)) and (not _in_scan_mode(cache_snapshot)):
            if key in _T1_probation:
                _T1_probation.pop(key, None)
            _T2_protected[key] = True
            placed_in_t2 = True
            # Seed stronger frequency on re-admission
            _freq[key] = max(_freq.get(key, 0), 3 if in_b2 else 2)
        else:
            # Stale ghost or during scan: insert into T1
            _T1_probation[key] = True
            _freq.setdefault(key, 0)
    else:
        # New to cache and ghosts: insert into probation (T1)
        if key in _T2_protected:
            # Rare desync; ensure consistency (shouldn't happen on miss)
            _T2_protected.move_to_end(key, last=True)
        else:
            _T1_probation[key] = True
        _freq.setdefault(key, 0)

    # Avoid duplicates across structures
    if placed_in_t2 and key in _T1_probation:
        _T1_probation.pop(key, None)
    if not placed_in_t2 and key in _T2_protected:
        _T2_protected.pop(key, None)

    # Sliding window tracking and scan mode upkeep (miss)
    _record_access(cache_snapshot, key, was_hit=False)
    _maybe_update_scan_mode(cache_snapshot)
    _ghost_trim()

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction.
    - Remove victim from its resident segment.
    - Add to corresponding ghost list (B1 if from probation, B2 if from protected) with timestamp.
    - Trim ghost lists to capacity.
    - Maintain fallback timestamp map and tiny frequency map.
    '''
    _ensure_capacity(cache_snapshot)
    victim_key = evicted_obj.key
    now = cache_snapshot.access_count

    was_t1 = victim_key in _T1_probation
    was_t2 = victim_key in _T2_protected

    # Remove from resident segments
    if was_t1:
        _T1_probation.pop(victim_key, None)
        _B1_ghost[victim_key] = now  # MRU with timestamp
    elif was_t2:
        _T2_protected.pop(victim_key, None)
        _B2_ghost[victim_key] = now  # MRU with timestamp
    else:
        # Unknown location; put in B1 by default
        _B1_ghost[victim_key] = now

    # Remove fallback timestamp and tiny frequency for evicted key
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