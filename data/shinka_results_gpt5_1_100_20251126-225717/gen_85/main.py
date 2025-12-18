# EVOLVE-BLOCK-START
"""Scan-aware ARC+SLRU with freshness-aware ghosts, momentum p-updates, and aging LRFU sampling"""

from collections import OrderedDict

# Segments (resident)
_T1_probation = OrderedDict()   # first-touch, recency-biased
_T2_protected = OrderedDict()   # multi-touch, frequency-biased

# Ghost histories (evicted keys) store eviction timestamps for freshness
_B1_ghost = OrderedDict()       # from T1: key -> evict_ts
_B2_ghost = OrderedDict()       # from T2: key -> evict_ts

# ARC's adaptive target (float) for T1 size
_p_target = 0.0
_cap_est = 0

# Fallback timestamp ledger and lightweight frequency
m_key_timestamp = dict()        # key -> last access time (for tie-breaking)
_freq = dict()                  # key -> small counter (saturating)
_last_age_tick = 0
# Track when a key first touched/probation touch to enforce time-bounded two-touch
_first_touch_ts = dict()
# Temporary "no-evict until tick" shield for freshly (re)admitted or promoted keys
_no_evict_until = dict()

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

# Tunables
_P_INIT_RATIO = 0.30             # initial share for T1
_FREQ_MAX = 7                    # 3-bit saturating counter
_FRESH_WINDOW_RATIO = 0.5        # ghost freshness window = 0.5 * cap
_SCAN_TRIGGER_INS = 0.7          # insert EWMA threshold
_SCAN_TRIGGER_HIT = 0.15         # hit EWMA threshold
_SCAN_WINDOW_MULT = 1.0          # scan window length ~= cap accesses
_P_COOLDOWN_DIV = 10             # min spacing between non-ghost p-updates (~cap/10 accesses) for faster adaptation
_CROSS_EVICT_FREQ_MARGIN = 1     # require a stricter freq gap to override segment choice

def _ensure_capacity(cache_snapshot):
    """Initialize capacity and clamp p within [0, cap]."""
    global _cap_est, _p_target
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

def _ghost_trim():
    """Bound ghosts by capacity."""
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

def _update_activity(is_hit, cache_snapshot):
    """Track recent hit/miss behavior and activate scan window if needed."""
    global _hit_ewma, _ins_ewma, _scan_until
    alpha = _EWMA_ALPHA
    _hit_ewma = (1.0 - alpha) * _hit_ewma + alpha * (1.0 if is_hit else 0.0)
    _ins_ewma = (1.0 - alpha) * _ins_ewma + alpha * (0.0 if is_hit else 1.0)
    if (_ins_ewma > _SCAN_TRIGGER_INS) and (_hit_ewma < _SCAN_TRIGGER_HIT):
        _scan_until = cache_snapshot.access_count + int(max(1, _SCAN_WINDOW_MULT * _cap_est))

def _adjust_p(sign, step, now, freshness_scale=1.0, force=False):
    """Momentum-based adjustment of ARC's p with cooldown and clamping."""
    global _p_target, _p_momentum, _p_last_update_tick
    # Throttle non-ghost adjustments to avoid oscillation
    if not force:
        cool = max(1, int(max(1, _cap_est) // max(1, _P_COOLDOWN_DIV)))
        if now - _p_last_update_tick < cool:
            return
    # Scale step by freshness and bound to 0.25*cap to avoid wild swings
    bounded = min(max(1.0, float(step) * float(freshness_scale)), max(1.0, 0.25 * float(_cap_est)))
    # Momentum update with clamp
    _p_momentum = 0.5 * _p_momentum + float(sign) * bounded
    max_mom = 0.25 * float(_cap_est)
    if _p_momentum > max_mom:
        _p_momentum = max_mom
    elif _p_momentum < -max_mom:
        _p_momentum = -max_mom
    # Apply and clamp p
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

def _score_key(k):
    """Compute victim score: lower is better (less frequent, older)."""
    return (_freq.get(k, 0), m_key_timestamp.get(k, 0))

def _pick_from(od, sample_n, cache_snapshot):
    """Pick victim from first few LRU entries by (freq asc, timestamp asc), skipping shielded keys if possible."""
    if not od:
        return None
    now = cache_snapshot.access_count
    cnt = 0
    best_allowed_k = None
    best_allowed_sc = None
    best_any_k = None
    best_any_sc = None
    for k in _lru_iter(od):
        if k not in cache_snapshot.cache:
            continue
        sc = _score_key(k)
        # Track best among all sampled keys
        if best_any_sc is None or sc < best_any_sc:
            best_any_sc = sc
            best_any_k = k
        # Prefer to avoid keys that are temporarily shielded
        if _no_evict_until.get(k, 0) > now:
            cnt += 1
            if cnt >= sample_n:
                break
            continue
        if best_allowed_sc is None or sc < best_allowed_sc:
            best_allowed_sc = sc
            best_allowed_k = k
        cnt += 1
        if cnt >= sample_n:
            break
    return best_allowed_k if best_allowed_k is not None else best_any_k

def _demote_protected_if_needed(cache_snapshot, avoid_key=None):
    """Keep T2 size within ARC target by demoting sampled cold entries to T1 MRU with a small T2 floor."""
    _ensure_capacity(cache_snapshot)
    cap = max(1, _cap_est)
    now = cache_snapshot.access_count
    t1_target = int(round(_p_target))
    t2_target = max(_cap_est - t1_target, 0)
    # Keep a small protected floor so T2 doesn't drain completely on transient p swings
    floor_t2 = max(0, int(0.1 * cap))
    if t2_target < floor_t2:
        t2_target = floor_t2
    while len(_T2_protected) > t2_target:
        # Sample first few LRU entries and demote the coldest by (freq asc, timestamp asc), avoiding shielded keys
        sample_n = 4
        cand = None
        best_sc = None
        fallback_k = None
        fallback_sc = None
        cnt = 0
        for k in _lru_iter(_T2_protected):
            if k == avoid_key or k not in cache_snapshot.cache:
                continue
            sc = _score_key(k)
            # track fallback regardless of shielding
            if fallback_sc is None or sc < fallback_sc:
                fallback_sc = sc
                fallback_k = k
            # skip temporarily shielded keys if possible
            if _no_evict_until.get(k, 0) > now:
                cnt += 1
                if cnt >= sample_n:
                    break
                continue
            if best_sc is None or sc < best_sc:
                best_sc = sc
                cand = k
            cnt += 1
            if cnt >= sample_n:
                break
        if cand is None:
            cand = fallback_k
        if cand is None:
            break
        _T2_protected.pop(cand, None)
        _T1_probation[cand] = True  # demoted MRU in T1
        # Start two-touch timer on demotion so it must prove itself again soon
        _first_touch_ts[cand] = now
        # Remove any stale shield on demoted item
        _no_evict_until.pop(cand, None)

def evict(cache_snapshot, obj):
    '''
    Evict using ARC replace with dynamic sampling and scan/guard bias:
    - Prefer T1 when |T1| > p or when upcoming key is in B2 and |T1| == p.
    - During scan/guard window, always prefer T1 if non-empty and avoid cross-segment overrides.
    - Cross-segment override: pick the globally colder candidate by (freq, age), with dynamic margin and min segment size.
    - Avoid evicting from a tiny protected set (keep a small protected floor).
    - Incoming-aware safeguard: avoid evicting items much hotter than the incoming key when an alternative exists.
    '''
    _ensure_capacity(cache_snapshot)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    x_in_b2 = (obj is not None) and (obj.key in _B2_ghost)
    p_int = int(round(_p_target))
    choose_t1 = (t1_size >= 1) and ((x_in_b2 and t1_size == p_int) or (t1_size > _p_target))

    cap = max(1, _cap_est)
    in_scan = cache_snapshot.access_count <= _scan_until
    in_guard = cache_snapshot.access_count <= _guard_until

    # Scan/guard bias: keep evictions in probation when scanning
    if (in_scan or in_guard) and t1_size > 0:
        choose_t1 = True

    # Avoid evicting from a tiny protected set (use dynamic floor when locality is good)
    prot_floor = int(((0.15 if _hit_ewma > 0.35 else 0.1) * cap))
    if (not choose_t1) and t2_size <= prot_floor and t1_size > 0:
        choose_t1 = True

    # Adaptive sampling sizes based on pressure and scan
    t1_pressure = (t1_size > _p_target + 0.1 * cap) or in_scan
    t2_pressure = (t2_size > (cap - int(round(_p_target)))) or False

    T1_SAMPLE = 1 if t1_pressure else 2
    if in_scan:
        T1_SAMPLE = 1
    T2_SAMPLE = 5 if t2_pressure else 3
    if _hit_ewma < 0.2:
        T2_SAMPLE = max(2, T2_SAMPLE - 1)

    # Sample candidates from both segments
    cand_t1 = _pick_from(_T1_probation, T1_SAMPLE, cache_snapshot) if t1_size > 0 else None
    cand_t2 = _pick_from(_T2_protected, T2_SAMPLE, cache_snapshot) if t2_size > 0 else None

    # Initial choice by ARC
    if choose_t1:
        victim_key = cand_t1 if cand_t1 is not None else cand_t2
    else:
        victim_key = cand_t2 if cand_t2 is not None else cand_t1

    # Cross-segment override: prefer globally colder by (freq asc, timestamp asc), but not during scan/guard
    if (not in_scan) and (not in_guard) and cand_t1 is not None and cand_t2 is not None:
        sc1 = _score_key(cand_t1)
        sc2 = _score_key(cand_t2)
        # Dynamic min segment size and frequency margin
        dyn_min_seg = max(1, int(0.2 * cap), int(0.5 * min(len(_T1_probation), len(_T2_protected))))
        cross_margin = _CROSS_EVICT_FREQ_MARGIN
        if _hit_ewma < 0.2:
            cross_margin += 1
        elif _hit_ewma > 0.35:
            cross_margin = max(0, cross_margin - 1)

        if choose_t1 and (sc2 < sc1) and (len(_T2_protected) > dyn_min_seg):
            victim_key = cand_t2
        elif (not choose_t1) and (sc1 < sc2) and (len(_T1_probation) > dyn_min_seg):
            victim_key = cand_t1
        else:
            f1, f2 = sc1[0], sc2[0]
            if choose_t1 and (f2 + cross_margin < f1) and (len(_T2_protected) > dyn_min_seg):
                victim_key = cand_t2
            elif (not choose_t1) and (f1 + cross_margin < f2) and (len(_T1_probation) > dyn_min_seg):
                victim_key = cand_t1

        # Incoming-aware safeguard: if chosen victim is much hotter than incoming, switch to the other cand if it is not
        inc_f = _freq.get(obj.key, 0) if (obj is not None) else 0
        if victim_key == cand_t2:
            if f2 > inc_f + cross_margin and f1 <= inc_f + cross_margin and (len(_T1_probation) > dyn_min_seg):
                victim_key = cand_t1
        elif victim_key == cand_t1:
            if f1 > inc_f + cross_margin and f2 <= inc_f + cross_margin and (len(_T2_protected) > dyn_min_seg):
                victim_key = cand_t2

    if victim_key is None:
        victim_key = _fallback_choose(cache_snapshot)
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On hit:
    - Update EWMA and age frequencies.
    - Increment frequency (saturating).
    - Two-touch promotion from T1 is time-bounded when locality is poor/scan or T1 is over target.
    - Otherwise: first hit in T1 promotes to T2.
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

    cap = max(1, _cap_est)
    shield_span = max(1, int((0.33 if _hit_ewma > 0.35 else 0.25) * cap))

    fresh_window = max(1, int(_FRESH_WINDOW_RATIO * _cap_est))
    in_scan = now <= _scan_until
    t1_over = len(_T1_probation) > int(round(_p_target))
    poor_locality = (_hit_ewma < 0.2)
    require_two_touch = in_scan or t1_over or poor_locality

    promoted = False

    if key in _T2_protected:
        _T2_protected.move_to_end(key, last=True)
        _first_touch_ts.pop(key, None)
        # Refresh shield for genuinely hot items
        _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
    elif key in _T1_probation:
        if require_two_touch:
            first_ts = _first_touch_ts.get(key, None)
            if first_ts is not None:
                if (now - first_ts) <= fresh_window:
                    # Promote to protected on timely second touch
                    _T1_probation.pop(key, None)
                    _T2_protected[key] = True
                    _first_touch_ts.pop(key, None)
                    _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
                    promoted = True
                else:
                    # Late second touch: reset stale frequency and restart two-touch window
                    _freq[key] = min(_freq.get(key, 0), 1)
                    _first_touch_ts[key] = now
                    _T1_probation.move_to_end(key, last=True)
            else:
                # Start two-touch window and keep in T1 MRU
                _first_touch_ts[key] = now
                _T1_probation.move_to_end(key, last=True)
        else:
            # Immediate promotion in high-locality phases
            _T1_probation.pop(key, None)
            _T2_protected[key] = True
            _first_touch_ts.pop(key, None)
            _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
            promoted = True
    else:
        # Metadata miss: treat as hot and place in T2
        _T2_protected[key] = True
        _first_touch_ts.pop(key, None)
        _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
        promoted = True

    # Gentle p adaptation on promotions during good locality (cooldowned)
    if promoted and not in_scan:
        try:
            _adjust_p(-1, max(1.0, 0.05 * float(_cap_est)), now, force=False)
        except Exception:
            pass

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
    - Update EWMA and age frequencies.
    - If key in ghosts: momentum-adjust p; fresh ghosts re-admit to T2 (seed freq with freshness), stale to T1.
    - Else: insert to T1; during guard/scan or poor locality, place at T1 LRU; gently lower p in these phases (with cooldown).
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_activity(False, cache_snapshot)

    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    fresh_window = max(1, int(_FRESH_WINDOW_RATIO * _cap_est))
    cap = max(1, _cap_est)
    shield_span = max(1, int((0.33 if _hit_ewma > 0.35 else 0.25) * cap))
    in_scan = now <= _scan_until

    if in_b1 or in_b2:
        # Compute step based on opposing ghost sizes with scan-aware weighting
        if in_b1:
            step = max(1.0, float(len(_B2_ghost)) / max(1.0, float(len(_B1_ghost))))
            if in_scan:
                step *= 0.5  # damp B1-driven increases during scans
            ev_ts = _B1_ghost.get(key, None)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            w = max(0.0, 1.0 - (age / float(fresh_window)))
            fresh = age <= fresh_window
            _adjust_p(+1, step, now, freshness_scale=(1.2 if fresh else 1.0), force=True)
            _B1_ghost.pop(key, None)
            if fresh and w >= 0.5:
                # Admit to T2 as recently valuable
                _T2_protected[key] = True
                _freq[key] = max(_freq.get(key, 0), min(_FREQ_MAX, 1 + int(round(4.0 * w))))
                _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
                _first_touch_ts[key] = now
                _freq[key] = _freq.get(key, 0)
        else:
            step = max(1.0, float(len(_B1_ghost)) / max(1.0, float(len(_B2_ghost))))
            if in_scan:
                step *= 1.2  # amplify B2-driven decreases during scans
            ev_ts = _B2_ghost.get(key, None)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            w = max(0.0, 1.0 - (age / float(fresh_window)))
            fresh = age <= fresh_window
            _adjust_p(-1, step, now, freshness_scale=(1.2 if fresh else 1.0), force=True)
            _B2_ghost.pop(key, None)
            if fresh and w >= 0.5:
                _T2_protected[key] = True
                _freq[key] = max(_freq.get(key, 0), min(_FREQ_MAX, 1 + int(round(4.0 * w))))
                _no_evict_until[key] = max(_no_evict_until.get(key, 0), now + shield_span)
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
                _first_touch_ts[key] = now
                _freq[key] = _freq.get(key, 0)
    else:
        # New key: insert into T1
        _T1_probation[key] = True
        _first_touch_ts[key] = now
        _freq[key] = _freq.get(key, 0)
        # Guard, scan and poor-locality handling: bias newcomer colder
        t1_over = len(_T1_probation) > int(round(_p_target))
        poor_locality = (_hit_ewma < 0.2)
        if (_last_victim_strength >= _VICTIM_GUARD_THRESH) or in_scan or poor_locality or t1_over:
            _T1_probation.move_to_end(key, last=False)
            # Gently lower p in scan or poor-locality phases to keep pressure in T1 (cooldowned)
            if in_scan or poor_locality:
                _adjust_p(-1, max(1.0, 0.08 * float(_cap_est)), now, force=False)

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
    - Track victim strength and set a short admission guard when a strong T2 victim is evicted.
    - Clean frequency and timestamp entries.
    '''
    _ensure_capacity(cache_snapshot)
    key = evicted_obj.key
    now = cache_snapshot.access_count

    was_t1 = key in _T1_probation
    was_t2 = key in _T2_protected

    fval = _freq.get(key, 0)
    strength = float(fval) + (2.0 if was_t2 else 0.0)
    global _last_victim_strength, _guard_until
    _last_victim_strength = strength

    if was_t1:
        _T1_probation.pop(key, None)
        _B1_ghost[key] = now
    elif was_t2:
        _T2_protected.pop(key, None)
        _B2_ghost[key] = now
        # Stronger guard when a very hot protected victim was evicted
        if fval >= 3:
            _guard_until = now + max(1, _cap_est // 2)
        elif fval >= 2:
            _guard_until = now + max(1, _cap_est // 3)
    else:
        # Unknown residency; default to B1 ghost
        _B1_ghost[key] = now

    m_key_timestamp.pop(key, None)
    _first_touch_ts.pop(key, None)
    _no_evict_until.pop(key, None)
    _freq.pop(key, None)
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