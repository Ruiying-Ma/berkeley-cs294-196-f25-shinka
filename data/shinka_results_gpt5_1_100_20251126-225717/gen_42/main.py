# EVOLVE-BLOCK-START
"""Adaptive ARC-like cache eviction with SLRU segments, SLFU, and scan detection"""

from collections import OrderedDict, deque

# Segment structures
_T1_probation = OrderedDict()   # keys in cache, 1st-touch (recency-biased), oldest->newest
_T2_protected = OrderedDict()   # keys in cache, 2nd+ touch (frequency-biased), oldest->newest

# Ghost history (recently evicted) with last-evict timestamp for freshness
_B1_ghost = OrderedDict()       # evicted from T1: key -> last_evicted_ts
_B2_ghost = OrderedDict()       # evicted from T2: key -> last_evicted_ts

# Adaptive target size for probation (ARC's p). Float to allow smooth adjust.
_p_target = 0.0

# Estimated capacity (number of objects). Initialize lazily.
_cap_est = 0

# Fallback LRU timestamps if metadata desync occurs
m_key_timestamp = dict()

# Lightweight per-key frequency counter (tiny saturating LFU)
_freq = dict()  # key -> int in [0..7]
_FREQ_MAX = 7
_last_age_tick = 0

# Admission guard based on last victim "strength"
_last_victim_strength = 0.0
_VICTIM_GUARD_THRESH = 2.0  # if last victim was strong, down-seed next newcomer
_guard_until = 0

# Sliding window for scan detection
_win_size = 0
_win_hits = deque()             # 1 for hit, 0 for miss
_win_keys = deque()             # keys in window
_win_key_counts = dict()        # key -> count in window
_unique_in_window = 0
_scan_mode_until = 0
_last_scan_adjust_at = 0

# Eviction sampling (number of LRU candidates to compare by frequency)
_T1_SAMPLE = 2
_T2_SAMPLE = 3

# Tunable parameters
_P_INIT_RATIO = 0.3  # initial share for probation (T1)
_SCAN_HIT_THRESH = 0.2
_SCAN_UNIQUE_THRESH = 0.6

def _ensure_capacity(cache_snapshot):
    """Initialize or update capacity estimate and clamp p, init window size."""
    global _cap_est, _p_target, _win_size
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
    # Initialize sliding window size once
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

def _maybe_age(cache_snapshot):
    """Periodically age tiny LFU frequencies to avoid stale bias."""
    global _last_age_tick, _freq
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    if now - _last_age_tick >= max(64, _cap_est):
        for k in list(_freq.keys()):
            newf = _freq.get(k, 0) // 2
            if newf <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = newf
        _last_age_tick = now

def _get_targets(cache_snapshot):
    """Compute ARC targets from p: T1 target = round(p), T2 target = cap - T1 target."""
    _ensure_capacity(cache_snapshot)
    t1_target = int(round(_p_target))
    t2_target = max(_cap_est - t1_target, 0)
    return t1_target, t2_target

def _demote_protected_if_needed(cache_snapshot, avoid_key=None):
    """Ensure protected size does not exceed its ARC target by demoting LRU to T1."""
    _, t2_target = _get_targets(cache_snapshot)
    if t2_target <= 0:
        return
    while len(_T2_protected) > t2_target:
        chosen = None
        for k in _T2_protected.keys():
            if avoid_key is not None and k == avoid_key:
                continue
            chosen = k
            break
        if chosen is None:
            break
        _T2_protected.pop(chosen, None)
        _T1_probation[chosen] = True  # demoted to MRU in T1

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

def _t2_sample_size():
    """Adaptive sampling size for T2 victim selection."""
    _, t2_target = _get_targets(None)
    crowded = len(_T2_protected) > t2_target
    return 5 if crowded else _T2_SAMPLE

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
    hit_rate = (sum(_win_hits) / float(total)) if total > 0 else 0.0
    unique_rate = (_unique_in_window / float(len(_win_keys))) if _win_keys else 0.0
    now = cache_snapshot.access_count
    if (unique_rate > _SCAN_UNIQUE_THRESH) and (hit_rate < _SCAN_HIT_THRESH):
        _scan_mode_until = max(_scan_mode_until, now + _win_size)

def _in_scan_mode(cache_snapshot):
    return cache_snapshot.access_count < _scan_mode_until

def evict(cache_snapshot, obj):
    '''
    Choose victim key using adaptive ARC-like policy with scan bias and tiny-LFU scoring.
    REPLACE(x): if |T1|>=1 and ((x in B2 and |T1| == p) or |T1| > p) -> evict from T1 else from T2.
    During scans, bias to evict from T1 to protect T2.
    Within chosen segment, sample a few LRU candidates and pick the one with smallest (freq, timestamp).
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)

    # Keep segment metadata consistent with actual cache content
    for d in (_T1_probation, _T2_protected):
        for k in list(d.keys()):
            if k not in cache_snapshot.cache:
                d.pop(k, None)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    x_in_b2 = (obj is not None) and (obj.key in _B2_ghost)
    p_int = int(round(_p_target))

    # Scan bias: prefer to evict from T1 when scanning
    if _in_scan_mode(cache_snapshot) and t1_size > 0:
        # LRU from T1
        for k in _T1_probation.keys():
            if k in cache_snapshot.cache:
                return k

    choose_t1 = (t1_size >= 1) and ((x_in_b2 and t1_size == p_int) or (t1_size > _p_target) or (t2_size == 0))

    # Dynamic sampling based on segment pressure
    _, t2_target = _get_targets(cache_snapshot)
    t1_pressure = t1_size > (int(round(_p_target)) + max(1, _cap_est // 10))
    t1_sample = 1 if t1_pressure else _T1_SAMPLE
    t2_sample = _t2_sample_size() if t2_size > t2_target else _T2_SAMPLE

    def _pick_from(od, sample_n):
        if not od:
            return None
        candidates = []
        for k in od.keys():
            if k in cache_snapshot.cache:
                candidates.append(k)
                if len(candidates) >= sample_n:
                    break
        if not candidates:
            return None
        now = cache_snapshot.access_count
        def score(k):
            # Lower freq better; older timestamp better
            return (_freq.get(k, 0), m_key_timestamp.get(k, now))
        return min(candidates, key=score)

    victim_key = None
    if choose_t1 and t1_size > 0:
        victim_key = _pick_from(_T1_probation, t1_sample)
    if victim_key is None and t2_size > 0:
        victim_key = _pick_from(_T2_protected, t2_sample)
    if victim_key is None and t1_size > 0:
        victim_key = _pick_from(_T1_probation, t1_sample)
    if victim_key is None:
        # Fallback to global LRU if metadata desync
        victim_key = _fallback_choose(cache_snapshot)
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after cache hit.
    - If hit in probation (T1), promote to protected (T2) unless scan mode requires two touches.
    - If hit in protected, refresh recency.
    - Maintain fallback timestamp map and per-key frequency, update scan stats.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    # Update fallback LRU timestamp
    m_key_timestamp[key] = now
    # Increment tiny LFU frequency with saturation
    curf = _freq.get(key, 0)
    _freq[key] = curf + 1 if curf < _FREQ_MAX else _FREQ_MAX

    if key in _T2_protected:
        # Refresh to MRU
        _T2_protected.move_to_end(key, last=True)
    elif key in _T1_probation:
        # In scan mode, require two touches before promotion (freq >= 2)
        if _in_scan_mode(cache_snapshot) and _freq.get(key, 0) < 2:
            _T1_probation.move_to_end(key, last=True)
        else:
            _T1_probation.pop(key, None)
            _T2_protected[key] = True  # insert as MRU
    else:
        # Metadata miss but present in cache; err on preserving it
        _T2_protected[key] = True

    # Enforce protected target by demoting its LRU if needed
    _demote_protected_if_needed(cache_snapshot, avoid_key=key)

    # Clean ghosts if re-referenced
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)
    _ghost_trim()

    # Scan window maintenance
    _record_access(cache_snapshot, key, was_hit=True)
    _maybe_update_scan_mode(cache_snapshot)

    # In scan mode, gradually tilt p downward to resist pollution
    global _last_scan_adjust_at, _p_target
    if _in_scan_mode(cache_snapshot) and now - _last_scan_adjust_at >= max(32, _cap_est // 2):
        step = max(1.0, float(len(_B2_ghost)) / max(1.0, float(len(_B1_ghost))))
        _p_target = max(0.0, _p_target - min(step, 0.25 * float(_cap_est)))
        _last_scan_adjust_at = now

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata on insertion (cache miss path).
    - If the key is in ghost lists, adjust p (ARC adaptation) and insert into protected only if ghost is fresh and not in scan mode.
    - Otherwise insert into probation as MRU, unless guarded by a strong last victim (insert at LRU).
    - Maintain fallback timestamp map, seed tiny LFU, and update scan stats.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    if in_b1 or in_b2:
        # ARC adaptation of p with freshness weighting
        global _p_target
        if in_b1:
            last = _B1_ghost.get(key, None)
            _B1_ghost.pop(key, None)
            # Favor recency: increase p
            other = len(_B2_ghost)
            this = len(_B1_ghost) + 1
            base = max(1.0, float(other) / max(1.0, float(this)))
            age = now - last if isinstance(last, int) else _cap_est * 10
            w = 1.5 if age <= max(1, _cap_est // 2) else 1.0
            _p_target = min(float(_cap_est), _p_target + min(base * w, 0.25 * float(_cap_est)))
        else:
            last = _B2_ghost.get(key, None)
            _B2_ghost.pop(key, None)
            # Favor frequency: decrease p
            other = len(_B1_ghost)
            this = len(_B2_ghost) + 1
            base = max(1.0, float(other) / max(1.0, float(this)))
            age = now - last if isinstance(last, int) else _cap_est * 10
            w = 1.5 if age <= max(1, _cap_est // 2) else 1.0
            _p_target = max(0.0, _p_target - min(base * w, 0.25 * float(_cap_est)))

        # Admission: only place directly into T2 if ghost is fresh and not in scan mode
        age = now - last if isinstance(last, int) else _cap_est * 10
        if (age <= max(1, _cap_est // 2)) and (not _in_scan_mode(cache_snapshot)):
            if key in _T1_probation:
                _T1_probation.pop(key, None)
            _T2_protected[key] = True
            # Seed stronger frequency on re-admission
            _freq[key] = max(_freq.get(key, 0), 3 if in_b2 else 2)
            # Keep protected within its target by demoting its LRU if necessary
            _demote_protected_if_needed(cache_snapshot, avoid_key=key)
        else:
            # Stale ghost or during scan: insert into T1
            _T1_probation[key] = True
            _freq.setdefault(key, 0)
    else:
        # New to cache and ghosts: insert into probation (T1)
        if key in _T2_protected:
            # Rare desync; ensure consistency
            _T2_protected.move_to_end(key, last=True)
        else:
            _T1_probation[key] = True
            # Admission guard: if last victim was strong OR we are within guard window, bias newcomer cold (place at LRU)
            if (_last_victim_strength >= _VICTIM_GUARD_THRESH) or (now <= _guard_until):
                _T1_probation.move_to_end(key, last=False)
        # Seed minimal frequency for new items
        _freq.setdefault(key, 0)

    # Avoid duplicates across structures
    if key in _T1_probation and key in _T2_protected:
        _T1_probation.pop(key, None)
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)
    _ghost_trim()

    # Sliding window tracking and scan mode upkeep (miss)
    _record_access(cache_snapshot, key, was_hit=False)
    _maybe_update_scan_mode(cache_snapshot)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after eviction.
    - Remove victim from its resident segment.
    - Add to corresponding ghost list (B1 if from probation, B2 if from protected) with timestamp.
    - Track last victim strength for admission guard and enable a short guard window if needed.
    - Trim ghost lists to capacity and clean timestamps/frequency.
    '''
    _ensure_capacity(cache_snapshot)
    victim_key = evicted_obj.key
    now = cache_snapshot.access_count

    was_t1 = victim_key in _T1_probation
    was_t2 = victim_key in _T2_protected

    # Track strength of the evicted item before removing counters
    fval = _freq.get(victim_key, 0)
    base_strength = float(fval)
    if was_t2:
        base_strength += 2.0  # extra credit for protected residency
    global _last_victim_strength, _guard_until
    _last_victim_strength = base_strength

    # Remove from resident segments and add to ghosts with timestamp
    if was_t1:
        _T1_probation.pop(victim_key, None)
        _B1_ghost[victim_key] = now  # MRU with timestamp
    elif was_t2:
        _T2_protected.pop(victim_key, None)
        _B2_ghost[victim_key] = now  # MRU with timestamp
        # If we had to evict a strong protected item, enable a short guard window
        if fval >= 2:
            _guard_until = now + max(1, _cap_est // 2)
    else:
        # Unknown location; put in B1 by default
        _B1_ghost[victim_key] = now

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