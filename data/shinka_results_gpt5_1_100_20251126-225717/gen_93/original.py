# EVOLVE-BLOCK-START
"""ARC + decayed-frequency hybrid with admission-guarded eviction and scan-aware two-touch gating"""

from collections import OrderedDict

# Segments (resident)
_T1_probation = OrderedDict()   # first-touch, recency-biased (LRU->MRU order)
_T2_protected = OrderedDict()   # multi-touch, frequency-biased (LRU->MRU order)

# Ghost histories (evicted keys) store eviction timestamps for freshness
_B1_ghost = OrderedDict()       # from T1: key -> evict_ts
_B2_ghost = OrderedDict()       # from T2: key -> evict_ts

# ARC's adaptive target (float) for T1 size
_p_target = 0.0
_cap_est = 0

# Recency timestamp (access time) and a small saturating counter (kept for guard heuristics)
m_key_timestamp = dict()        # key -> last access time
_freq = dict()                  # key -> small counter (saturating)
_last_age_tick = 0

# Decayed-frequency (TinyLFU-like) counters with epoch-based halving
_refcnt = {}                    # key -> (count, epoch)
_epoch_df = 0
_last_epoch_tick_df = 0
_DECAY_WINDOW = 128             # accesses per epoch; tied to capacity

def _decayed_score(key):
    """Return decayed reference score for a key."""
    ce = _refcnt.get(key)
    if ce is None:
        return 0
    c, e = ce
    de = _epoch_df - e
    if de > 0:
        c = c >> min(6, de)
    return max(0, c)

def _inc_decayed(key):
    """Increment decayed reference count for a key (epoch-aware)."""
    c, e = _refcnt.get(key, (0, _epoch_df))
    if e != _epoch_df:
        c = c >> min(6, _epoch_df - e)
        e = _epoch_df
    _refcnt[key] = (min(c + 1, 1 << 30), e)

# Admission guard based on last victim strength and a short scan window
_last_victim_strength = 0.0
_VICTIM_GUARD_THRESH = 2.0
_guard_until = 0

# Scan detection and momentum for p updates
_hit_ewma = 0.0
_ins_ewma = 0.0
_EWMA_ALPHA = 0.05
_scan_until = 0
_p_momentum = 0.0
_p_last_update_tick = 0

# Two-touch gating memory (epoch-based)
_touched_once = {}  # key -> epoch when first touched in T1

# Tunables
_P_INIT_RATIO = 0.30             # initial share for T1
_FREQ_MAX = 7                    # 3-bit saturating counter
_FRESH_WINDOW_RATIO = 0.5        # ghost freshness window = 0.5 * cap
_SCAN_TRIGGER_INS = 0.7          # insert EWMA threshold
_SCAN_TRIGGER_HIT = 0.15         # hit EWMA threshold
_SCAN_WINDOW_MULT = 1.0          # scan window length ~= cap accesses

def _ensure_capacity(cache_snapshot):
    """Initialize capacity and clamp p within [0, cap]. Also tie decay window to capacity."""
    global _cap_est, _p_target, _DECAY_WINDOW
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    if _cap_est <= 0:
        _cap_est = max(1, len(cache_snapshot.cache))
    if _p_target == 0.0 and not _T1_probation and not _T2_protected and not _B1_ghost and not _B2_ghost:
        _p_target = min(float(_cap_est), max(0.0, float(_cap_est) * _P_INIT_RATIO))
    if _p_target < 0.0:
        _p_target = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
    # Decay window aligned with capacity to reflect working-set half-life
    _DECAY_WINDOW = max(64, int(_cap_est))

def _ghost_trim():
    """Bound ghosts by capacity."""
    while len(_B1_ghost) > _cap_est:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > _cap_est:
        _B2_ghost.popitem(last=False)

def _maybe_age(cache_snapshot):
    """Periodically age frequency counters and advance the decayed epoch."""
    global _last_age_tick, _epoch_df, _last_epoch_tick_df
    _ensure_capacity(cache_snapshot)
    now = cache_snapshot.access_count
    # Age simple saturating counters
    if now - _last_age_tick >= max(1, _cap_est):
        for k in list(_freq.keys()):
            newf = _freq.get(k, 0) // 2
            if newf <= 0:
                _freq.pop(k, None)
            else:
                _freq[k] = newf
        _last_age_tick = now
    # Advance decayed-frequency epoch
    if now - _last_epoch_tick_df >= _DECAY_WINDOW:
        _epoch_df += 1
        _last_epoch_tick_df = now

def _update_activity(is_hit, cache_snapshot):
    """Track recent hit/miss behavior and activate scan window if needed."""
    global _hit_ewma, _ins_ewma, _scan_until
    alpha = _EWMA_ALPHA
    _hit_ewma = (1.0 - alpha) * _hit_ewma + alpha * (1.0 if is_hit else 0.0)
    _ins_ewma = (1.0 - alpha) * _ins_ewma + alpha * (0.0 if is_hit else 1.0)
    if (_ins_ewma > _SCAN_TRIGGER_INS) and (_hit_ewma < _SCAN_TRIGGER_HIT):
        _scan_until = cache_snapshot.access_count + int(max(1, _SCAN_WINDOW_MULT * _cap_est))

def _adjust_p(sign, step, now, freshness_scale=1.0):
    """Momentum-based adjustment of ARC's p with clamping."""
    global _p_target, _p_momentum, _p_last_update_tick
    bounded = min(max(1.0, float(step) * float(freshness_scale)), max(1.0, 0.25 * float(_cap_est)))
    _p_momentum = 0.5 * _p_momentum + float(sign) * bounded
    _p_target += _p_momentum
    if _p_target < 0.0:
        _p_target = 0.0
        _p_momentum = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
        _p_momentum = 0.0
    _p_last_update_tick = now

def _fallback_choose(cache_snapshot):
    """LRU fallback using timestamps."""
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None
    known = [(k, m_key_timestamp.get(k, None)) for k in keys]
    known_ts = [x for x in known if x[1] is not None]
    if known_ts:
        return min(known_ts, key=lambda kv: kv[1])[0]
    return keys[0]

def _lru_iter(od):
    """Iterate keys from LRU to MRU for an OrderedDict."""
    for k in od.keys():
        yield k

def _pick_with_guard(od, sample_n, cache_snapshot, incoming_key):
    """Pick victim from LRU side with admission guard: avoid evicting hotter than incoming if possible."""
    if not od:
        return None
    inc_score = _decayed_score(incoming_key) if incoming_key is not None else 0
    cnt = 0
    best = None
    best_tuple = None
    for k in _lru_iter(od):
        if k not in cache_snapshot.cache:
            continue
        s = _decayed_score(k)
        hotter = 1 if s > inc_score else 0
        tup = (hotter, s, m_key_timestamp.get(k, 0))
        if best_tuple is None or tup < best_tuple:
            best_tuple = tup
            best = k
        cnt += 1
        if cnt >= sample_n:
            break
    return best

def _demote_protected_if_needed(cache_snapshot, avoid_key=None):
    """Keep T2 size within ARC target by demoting LRU entries to T1 MRU."""
    _ensure_capacity(cache_snapshot)
    t1_target = int(round(_p_target))
    t2_target = max(_cap_est - t1_target, 0)
    while len(_T2_protected) > t2_target:
        # LRU of T2
        lru = None
        for k in _lru_iter(_T2_protected):
            if k != avoid_key and k in cache_snapshot.cache:
                lru = k
                break
        if lru is None:
            break        # nothing to demote
        _T2_protected.pop(lru, None)
        _T1_probation[lru] = True  # demoted MRU in T1

def evict(cache_snapshot, obj):
    '''
    Evict using ARC replace with decayed-frequency, admission guard, and scan bias:
    - Prefer T1 when |T1| > p or when upcoming key is in B2 and |T1| == p.
    - During scan window, always prefer T1 if non-empty.
    - Within the chosen segment, sample a few LRU entries and pick (hotter_flag, decayed_score asc, timestamp asc).
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    x_in_b2 = (obj is not None) and (obj.key in _B2_ghost)
    choose_t1 = (t1_size >= 1) and ((x_in_b2 and t1_size == int(round(_p_target))) or (t1_size > _p_target))

    # Scan bias: keep evictions in probation when scanning
    if cache_snapshot.access_count <= _scan_until and t1_size > 0:
        choose_t1 = True

    # Adaptive sampling sizes based on pressure and scan
    cap = max(1, _cap_est)
    t1_pressure = (t1_size > _p_target + 0.1 * cap) or (cache_snapshot.access_count <= _scan_until)
    t2_pressure = (t2_size > (cap - int(round(_p_target)))) or False

    T1_SAMPLE = 1 if t1_pressure else 3
    if cache_snapshot.access_count <= _scan_until:
        T1_SAMPLE = 1
    T2_SAMPLE = 5 if t2_pressure else 3
    if _hit_ewma < 0.2:
        T2_SAMPLE = max(2, T2_SAMPLE - 1)

    incoming_key = obj.key if obj is not None else None

    victim_key = None
    if choose_t1 and t1_size > 0:
        victim_key = _pick_with_guard(_T1_probation, T1_SAMPLE, cache_snapshot, incoming_key)
    if victim_key is None and t2_size > 0:
        victim_key = _pick_with_guard(_T2_protected, T2_SAMPLE, cache_snapshot, incoming_key)
    if victim_key is None and t1_size > 0:
        victim_key = _pick_with_guard(_T1_probation, T1_SAMPLE, cache_snapshot, incoming_key)
    if victim_key is None:
        victim_key = _fallback_choose(cache_snapshot)
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On hit:
    - Update EWMA and age counters.
    - Increment decayed frequency and saturating counter.
    - In scan mode: require two touches in T1 before promotion (epoch-gated).
    - Otherwise: first hit in T1 promotes to T2 if already touched once, or clearly hot by decayed score, or recent B2 ghost.
    - Keep T2 within its ARC target via demotion.
    - Remove any ghost entries for this key.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_activity(True, cache_snapshot)

    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now
    _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 1)
    _inc_decayed(key)

    in_scan = now <= _scan_until
    fresh_window = max(1, int(_FRESH_WINDOW_RATIO * _cap_est))

    if key in _T2_protected:
        _T2_protected.move_to_end(key, last=True)
        _touched_once.pop(key, None)
    elif key in _T1_probation:
        last_epoch = _touched_once.get(key)
        promote = False
        if in_scan:
            if last_epoch is not None and (_epoch_df - last_epoch) <= 1:
                promote = True
            else:
                _touched_once[key] = _epoch_df
        else:
            hot_freq = (_decayed_score(key) >= 2)
            ev_ts = _B2_ghost.get(key, None)
            recent_b2 = (isinstance(ev_ts, int) and (now - ev_ts) <= fresh_window)
            if (last_epoch is not None) or hot_freq or recent_b2:
                promote = True
            else:
                _touched_once[key] = _epoch_df

        if promote:
            _touched_once.pop(key, None)
            _T1_probation.pop(key, None)
            _T2_protected[key] = True  # MRU in T2
        else:
            _T1_probation.move_to_end(key, last=True)
    else:
        # Metadata miss: treat as hot and place in T2
        _T2_protected[key] = True
        _touched_once.pop(key, None)

    _demote_protected_if_needed(cache_snapshot, avoid_key=key)

    # Ghost cleanup
    if key in _B1_ghost:
        _B1_ghost.pop(key, None)
    if key in _B2_ghost:
        _B2_ghost.pop(key, None)
    _ghost_trim()

def update_after_insert(cache_snapshot, obj):
    '''
    On miss and insert:
    - Update EWMA, age counters, and decayed frequency.
    - If key in ghosts: momentum-adjust p; fresh ghosts re-admit to T2 (seed decayed freq), stale to T1.
    - Else: insert to T1; during guard/scan, place at T1 LRU and gently lower p in scan to bias T1 evictions.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_activity(False, cache_snapshot)

    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now
    _inc_decayed(key)  # count the access producing this insert

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    fresh_window = max(1, int(_FRESH_WINDOW_RATIO * _cap_est))

    if in_b1 or in_b2:
        if in_b1:
            step = max(1.0, float(len(_B2_ghost)) / max(1.0, float(len(_B1_ghost))))
            ev_ts = _B1_ghost.get(key, None)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            fresh = age <= fresh_window
            _adjust_p(+1, step, now, freshness_scale=(1.2 if fresh else 1.0))
            _B1_ghost.pop(key, None)
            if fresh:
                _T2_protected[key] = True
                # Seed some frequency to stabilize against immediate eviction
                _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 2)
                _inc_decayed(key)
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
        else:
            step = max(1.0, float(len(_B1_ghost)) / max(1.0, float(len(_B2_ghost))))
            ev_ts = _B2_ghost.get(key, None)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            fresh = age <= fresh_window
            _adjust_p(-1, step, now, freshness_scale=(1.2 if fresh else 1.0))
            _B2_ghost.pop(key, None)
            if fresh:
                _T2_protected[key] = True
                _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 2)
                _inc_decayed(key)
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
    else:
        # New key: insert into T1
        _T1_probation[key] = True
        # Guard and scan handling: bias newcomer colder
        if (_last_victim_strength >= _VICTIM_GUARD_THRESH) or (now <= _scan_until):
            _T1_probation.move_to_end(key, last=False)
            # During scan, gently lower p to keep pressure in T1
            if now <= _scan_until:
                _adjust_p(-1, max(1.0, 0.1 * float(_cap_est)), now)

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
    After eviction:
    - Remove from resident segment and put into the appropriate ghost with timestamp.
    - Track victim strength (decayed) and set a short admission guard when a strong T2 victim is evicted.
    - Clean frequency, timestamp, and two-touch entries.
    '''
    _ensure_capacity(cache_snapshot)
    key = evicted_obj.key
    now = cache_snapshot.access_count

    was_t1 = key in _T1_probation
    was_t2 = key in _T2_protected

    fval_dec = _decayed_score(key)
    strength = float(fval_dec) + (2.0 if was_t2 else 0.0)
    global _last_victim_strength, _guard_until
    _last_victim_strength = strength

    if was_t1:
        _T1_probation.pop(key, None)
        _B1_ghost[key] = now
    elif was_t2:
        _T2_protected.pop(key, None)
        _B2_ghost[key] = now
        if fval_dec >= 2:
            _guard_until = now + max(1, _cap_est // 2)
    else:
        # Unknown residency; default to B1 ghost
        _B1_ghost[key] = now

    # Clean per-key state
    m_key_timestamp.pop(key, None)
    _freq.pop(key, None)
    _refcnt.pop(key, None)
    _touched_once.pop(key, None)

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