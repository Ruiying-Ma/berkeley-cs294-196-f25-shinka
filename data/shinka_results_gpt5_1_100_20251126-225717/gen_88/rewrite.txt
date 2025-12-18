# EVOLVE-BLOCK-START
"""SAFIR-ARC+TinyLFU: Scan-aware ARC with freshness-weighted ghosts, momentum p, and sampled-LRFU demotion"""

from collections import OrderedDict

# ---------------- Resident segments (LRU->MRU) ----------------
_T1_probation = OrderedDict()   # first-touch, recency-biased
_T2_protected = OrderedDict()   # multi-touch, frequency-biased

# ---------------- Ghost histories (key -> evict_ts) ----------------
_B1_ghost = OrderedDict()       # evicted from T1
_B2_ghost = OrderedDict()       # evicted from T2

# ---------------- ARC's adaptive target and capacity ----------------
_p_target = 0.0                 # target size (float) for T1
_cap_est = 0

# ---------------- Timestamps and light frequency ----------------
m_key_timestamp = dict()        # key -> last access time
_freq = dict()                  # small saturating counter for fast two-touch
_FREQ_MAX = 7
_last_age_tick = 0

# ---------------- TinyLFU decayed counters ----------------
_refcnt = {}                    # key -> (count, epoch)
_epoch_df = 0
_last_epoch_tick_df = 0
_DECAY_WINDOW = 128

def _decayed_score(key):
    ce = _refcnt.get(key)
    if ce is None:
        return 0
    c, e = ce
    de = _epoch_df - e
    if de > 0:
        c = c >> min(6, de)
    return max(0, c)

def _inc_decayed(key):
    c, e = _refcnt.get(key, (0, _epoch_df))
    if e != _epoch_df:
        c = c >> min(6, _epoch_df - e)
        e = _epoch_df
    _refcnt[key] = (min(c + 1, 1 << 30), e)

# ---------------- Freshness window and ghost reuse sampling ----------------
_fresh_window = 0
_ghost_age_samples = []

def _update_fresh_window_sample(age):
    global _fresh_window
    try:
        _ghost_age_samples.append(int(max(0, age)))
        if len(_ghost_age_samples) > max(1, _cap_est):
            del _ghost_age_samples[0:len(_ghost_age_samples) - _cap_est]
        if len(_ghost_age_samples) >= max(4, _cap_est // 2):
            arr = sorted(_ghost_age_samples)
            mid = len(arr) // 2
            median_age = arr[mid] if len(arr) % 2 == 1 else (arr[mid - 1] + arr[mid]) // 2
            lo = max(1, int(0.25 * _cap_est))
            hi = max(1, int(1.00 * _cap_est))
            _fresh_window = max(lo, min(hi, int(median_age)))
    except Exception:
        _fresh_window = max(1, int(0.5 * _cap_est))

# ---------------- Scan detection, guard, and p-momentum ----------------
_hit_ewma = 0.0
_ins_ewma = 0.0
_EWMA_ALPHA = 0.05
_SCAN_TRIGGER_INS = 0.7
_SCAN_TRIGGER_HIT = 0.15
_scan_until = 0

_last_victim_strength = 0.0
_VICTIM_GUARD_THRESH = 2.0
_guard_until = 0

_p_momentum = 0.0
_p_last_update_tick = 0
_p_last_sign = 0  # track sign flips

# ---------------- Tunables ----------------
_P_INIT_RATIO = 0.30
_GHOST_BOUND_MULT = 1           # bound each ghost list to ≈ cap
_BASE_T1_SAMPLE = 2
_BASE_T2_SAMPLE = 3

# Readmission demotion immunity
_no_demote_until = {}           # key -> access_count immunity deadline

# Two-touch timestamps to reset stale freq on late second touches
_first_touch_ts = {}            # key -> access_count of first touch in T1


# ---------------- Core utilities ----------------
def _ensure_capacity(cache_snapshot):
    """Initialize capacity, p clamping, decay window, and fresh window."""
    global _cap_est, _p_target, _DECAY_WINDOW, _fresh_window
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _cap_est = cap
    else:
        _cap_est = max(_cap_est, len(cache_snapshot.cache))
    if _cap_est <= 0:
        _cap_est = max(1, len(cache_snapshot.cache))

    # Initialize p on first use
    if _p_target == 0.0 and not _T1_probation and not _T2_protected and not _B1_ghost and not _B2_ghost:
        _p_target = min(float(_cap_est), max(0.0, float(_cap_est) * _P_INIT_RATIO))
    if _p_target < 0.0:
        _p_target = 0.0
        _p_momentum = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
        _p_momentum = 0.0

    # Decay window faster under scan/guard
    now = cache_snapshot.access_count
    if now <= max(_scan_until, _guard_until):
        _DECAY_WINDOW = max(32, int(_cap_est // 2) or 1)
    else:
        _DECAY_WINDOW = max(64, int(_cap_est))

    # Initialize fresh window if unset
    if _fresh_window <= 0:
        _fresh_window = max(1, int(0.5 * _cap_est))

def _ghost_trim():
    """Bound ghosts by capacity multiplier."""
    limit = max(1, _GHOST_BOUND_MULT * max(_cap_est, 1))
    while len(_B1_ghost) > limit:
        _B1_ghost.popitem(last=False)
    while len(_B2_ghost) > limit:
        _B2_ghost.popitem(last=False)

def _maybe_age(cache_snapshot):
    """Age small counters and advance TinyLFU epoch periodically."""
    global _last_age_tick, _epoch_df, _last_epoch_tick_df
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

    if now - _last_epoch_tick_df >= _DECAY_WINDOW:
        _epoch_df += 1
        _last_epoch_tick_df = now

def _update_activity(is_hit, cache_snapshot):
    """Update EWMAs, detect scans, and adjust alpha."""
    global _hit_ewma, _ins_ewma, _scan_until, _EWMA_ALPHA
    # Adaptive alpha: faster when scan-like behavior suspected
    tentative_alpha = 0.15 if (_ins_ewma > _SCAN_TRIGGER_INS and _hit_ewma < _SCAN_TRIGGER_HIT) else 0.05
    _EWMA_ALPHA = tentative_alpha

    alpha = _EWMA_ALPHA
    _hit_ewma = (1.0 - alpha) * _hit_ewma + alpha * (1.0 if is_hit else 0.0)
    _ins_ewma = (1.0 - alpha) * _ins_ewma + alpha * (0.0 if is_hit else 1.0)

    if (_ins_ewma > _SCAN_TRIGGER_INS) and (_hit_ewma < _SCAN_TRIGGER_HIT):
        _scan_until = cache_snapshot.access_count + int(max(1, 1.0 * _cap_est))

def _adjust_p(sign, step, src, now):
    """Sign-sensitive momentum update for ARC's p with per-source coefficients."""
    global _p_target, _p_momentum, _p_last_update_tick, _p_last_sign
    # Per-source momentum coefficient
    beta = 0.7 if src == "ghost" else 0.3
    bounded = min(max(0.5, float(step)), max(1.0, 0.25 * float(_cap_est)))

    # Decay momentum when sign flips to curb overshoot
    if _p_last_sign != 0 and (sign != _p_last_sign):
        _p_momentum *= 0.5
    _p_last_sign = sign

    # Momentum with source-dependent blending
    _p_momentum = (1.0 - beta) * _p_momentum + beta * float(sign) * bounded
    _p_target += _p_momentum

    if _p_target < 0.0:
        _p_target = 0.0
        _p_momentum = 0.0
    if _p_target > float(_cap_est):
        _p_target = float(_cap_est)
        _p_momentum = 0.0
    _p_last_update_tick = now


# ---------------- Helpers for victim selection and demotion ----------------
def _lru_iter(od):
    """Iterate keys from LRU to MRU for an OrderedDict."""
    for k in od.keys():
        yield k

def _pick_with_guard(od, sample_n, cache_snapshot, incoming_key):
    """Victim from LRU side with incoming-aware guard and LRFU signals."""
    if not od:
        return None
    inc_score = _decayed_score(incoming_key) if incoming_key is not None else 0
    cnt = 0
    best = None
    best_tuple = None
    now = cache_snapshot.access_count
    for k in _lru_iter(od):
        if k not in cache_snapshot.cache:
            continue
        s = _decayed_score(k)
        # hotter than incoming?
        hotter = 1 if s > inc_score else 0
        tup = (hotter, s, _freq.get(k, 0), m_key_timestamp.get(k, now), k)
        if best_tuple is None or tup < best_tuple:
            best_tuple = tup
            best = k
        cnt += 1
        if cnt >= sample_n:
            break
    return best

def _protected_floor():
    # Dynamic protected floor: 10% base, 15% when locality is good
    return 0.15 if _hit_ewma > 0.35 else 0.10

def _median(values):
    if not values:
        return 0
    arr = sorted(values)
    n = len(arr)
    mid = n // 2
    return arr[mid] if n % 2 else (arr[mid - 1] + arr[mid]) // 2

def _demote_protected_if_needed(cache_snapshot, avoid_key=None):
    """Keep T2 within target using sampled-LRFU demotion with immunity and floor."""
    _ensure_capacity(cache_snapshot)
    t1_target = int(round(_p_target))
    t2_target = max(_cap_est - t1_target, 0)
    floor = _protected_floor()
    t2_floor = max(0, int(floor * max(1, t2_target)))
    if t2_target < t2_floor:
        t2_target = t2_floor

    now = cache_snapshot.access_count
    while len(_T2_protected) > t2_target and len(_T2_protected) > t2_floor:
        # Sample a small set from T2 LRU; respect immunity for fresh readmissions
        sample = []
        cnt = 0
        for k in _lru_iter(_T2_protected):
            if k == avoid_key or k not in cache_snapshot.cache:
                continue
            if _no_demote_until.get(k, 0) > now:
                continue
            sample.append(k)
            cnt += 1
            if cnt >= 5:
                break
        if not sample:
            break
        # Compute LRFU-like metric for sample and demote only those <= median
        scores = [(_decayed_score(kk), _freq.get(kk, 0), m_key_timestamp.get(kk, 0), kk) for kk in sample]
        med_decayed = _median([x[0] for x in scores])
        # among those with decayed <= median, pick the coldest by (decayed, satfreq, older ts)
        cands = [x for x in scores if x[0] <= med_decayed]
        if not cands:
            cands = scores
        dec, sf, ts, coldest = min(cands, key=lambda x: (x[0], x[1], x[2]))
        _T2_protected.pop(coldest, None)
        _T1_probation[coldest] = True  # demoted MRU


# ---------------- Public API: evict + updates ----------------
def evict(cache_snapshot, obj):
    '''
    ARC replace with scan/guard bias, cross-segment override, and incoming-aware sampling:
    - Prefer evicting from T1 when |T1| > p or when upcoming key appears in B2 and |T1| == p.
    - Under scan/guard, prefer T1 when non-empty.
    - Within the chosen segment, sample LRU and pick min by (hotter_flag vs incoming, decayed, satfreq, recency).
    - Cross-segment override: avoid evicting genuinely hot protected items unless T1 is tiny; require margin when locality is poor.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)

    t1_size = len(_T1_probation)
    t2_size = len(_T2_protected)
    now = cache_snapshot.access_count
    in_scan = now <= max(_scan_until, _guard_until)

    incoming_key = obj.key if obj is not None else None
    x_in_b2 = (obj is not None) and (obj.key in _B2_ghost)

    # ARC replace baseline
    choose_t1 = (t1_size >= 1) and ((x_in_b2 and t1_size == int(round(_p_target))) or (t1_size > _p_target))
    if in_scan and t1_size > 0:
        choose_t1 = True

    # Adaptive sampling sizes
    cap = max(1, _cap_est)
    t1_pressure = (t1_size > _p_target + 0.1 * cap) or in_scan
    t2_pressure = (t2_size > (cap - int(round(_p_target))))
    T1_SAMPLE = 1 if t1_pressure else _BASE_T1_SAMPLE
    if in_scan:
        T1_SAMPLE = 1
    T2_SAMPLE = (_BASE_T2_SAMPLE + 1) if t2_pressure else _BASE_T2_SAMPLE
    if _hit_ewma < 0.2:
        T2_SAMPLE = max(2, T2_SAMPLE - 1)

    # Cross-segment override thresholds
    min_seg = max(1, int(0.2 * _cap_est), int(0.5 * max(1, min(len(_T1_probation), len(_T2_protected)))))
    freq_margin = 1 if _hit_ewma < 0.2 else 0  # require extra margin under poor locality

    victim_key = None
    if choose_t1 and t1_size > 0:
        victim_key = _pick_with_guard(_T1_probation, T1_SAMPLE, cache_snapshot, incoming_key)

    if victim_key is None and t2_size > 0:
        # Consider T2 only when T1 is small or incoming is clearly not cold vs T2's LRU sample
        if t1_size <= min_seg:
            victim_key = _pick_with_guard(_T2_protected, T2_SAMPLE, cache_snapshot, incoming_key)
        else:
            # Evaluate LRU of both and compare scores with margin
            cand_t1 = _pick_with_guard(_T1_probation, min(1, T1_SAMPLE), cache_snapshot, incoming_key)
            cand_t2 = _pick_with_guard(_T2_protected, min(2, T2_SAMPLE), cache_snapshot, incoming_key)
            if cand_t1 is not None and cand_t2 is not None:
                s1 = _decayed_score(cand_t1)
                s2 = _decayed_score(cand_t2)
                if s2 <= max(0, s1 - freq_margin):
                    victim_key = cand_t2
            if victim_key is None:
                victim_key = cand_t1 if cand_t1 is not None else cand_t2

    if victim_key is None and t1_size > 0:
        victim_key = _pick_with_guard(_T1_probation, T1_SAMPLE, cache_snapshot, incoming_key)

    if victim_key is None:
        # Fallback to global LRU by timestamps
        keys = list(cache_snapshot.cache.keys())
        if keys:
            victim_key = min(keys, key=lambda k: m_key_timestamp.get(k, 0))
    return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On hit:
    - Update EWMAs and aging; bump TinyLFU and small freq; refresh timestamp.
    - In T1: two-touch gating with stale reset. Require second touch within fresh_window during scans/poor locality;
      otherwise promote if hot (decayed>=2) or recent B2 ghost. Late second touches reset freq to 1 and keep in T1.
    - In T2: refresh MRU.
    - Keep T2 within target via sampled-LRFU demotion with floor and immunity.
    - Clean ghosts for this key.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_activity(True, cache_snapshot)

    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now
    _freq[key] = min(_FREQ_MAX, _freq.get(key, 0) + 1)
    _inc_decayed(key)

    in_scan = now <= max(_scan_until, _guard_until)
    fresh_window = max(1, int(_fresh_window))

    if key in _T2_protected:
        _T2_protected.move_to_end(key, last=True)
        _first_touch_ts.pop(key, None)
    elif key in _T1_probation:
        hot_decayed = (_decayed_score(key) >= 2)
        ev_ts_b2 = _B2_ghost.get(key, None)
        recent_b2 = isinstance(ev_ts_b2, int) and ((now - ev_ts_b2) <= fresh_window)
        strict = in_scan or (_hit_ewma < 0.25) or (len(_T1_probation) > int(round(_p_target)))

        # Two-touch timing and stale reset
        ft = _first_touch_ts.get(key)
        if ft is None:
            _first_touch_ts[key] = now

        promote = False
        if strict:
            if ft is not None and (now - ft) <= fresh_window:
                if _freq.get(key, 0) >= 2 or hot_decayed or recent_b2:
                    promote = True
            elif hot_decayed or recent_b2:
                promote = True
            else:
                # Late second touch: reset stale frequency to avoid old bias
                _freq[key] = 1
                _first_touch_ts[key] = now
                _T1_probation.move_to_end(key, last=True)
        else:
            if _freq.get(key, 0) >= 2 or hot_decayed or recent_b2:
                promote = True
            else:
                _T1_probation.move_to_end(key, last=True)

        if promote:
            _T1_probation.pop(key, None)
            _T2_protected[key] = True  # MRU
            _first_touch_ts.pop(key, None)
    else:
        # Treat as hot if metadata missing
        _T2_protected[key] = True
        _first_touch_ts.pop(key, None)

    _demote_protected_if_needed(cache_snapshot, avoid_key=key)

    # Ghost cleanup
    _B1_ghost.pop(key, None)
    _B2_ghost.pop(key, None)
    _ghost_trim()

def update_after_insert(cache_snapshot, obj):
    '''
    On miss/insert:
    - Update EWMAs and aging; bump TinyLFU on the miss access.
    - If in ghosts: compute age and freshness w = max(0, 1 - age/fresh_window).
      Update ARC p with momentum scaled per source. During scans, damp B1-driven increases (×0.5)
      and amplify B2-driven decreases (×1.2). Admit to T2 if w ≥ 0.5 else T1; seed _freq = max(_freq, 1+round(4w)).
      Record reuse ages to adapt fresh_window and grant demotion immunity for fresh readmissions.
    - Else new: insert to T1 MRU; during scan/guard, place at T1 LRU and gently lower p to bias T1 evictions.
    '''
    _ensure_capacity(cache_snapshot)
    _maybe_age(cache_snapshot)
    _update_activity(False, cache_snapshot)

    key = obj.key
    now = cache_snapshot.access_count
    m_key_timestamp[key] = now
    _inc_decayed(key)

    in_scan = now <= max(_scan_until, _guard_until)
    fresh_window = max(1, int(_fresh_window))

    in_b1 = key in _B1_ghost
    in_b2 = key in _B2_ghost

    if in_b1 or in_b2:
        if in_b1:
            ev_ts = _B1_ghost.get(key)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            _update_fresh_window_sample(age)
            w = max(0.0, 1.0 - float(age) / float(fresh_window))
            # ARC p toward recency; damp during scans
            base_step = max(1.0, float(len(_B2_ghost)) / max(1.0, float(len(_B1_ghost))))
            step = base_step * (1.0 + 2.0 * w)
            if in_scan:
                step *= 0.5
            _adjust_p(+1, step, "ghost", now)
            _B1_ghost.pop(key, None)
            # Admission and seeding
            _freq[key] = max(_freq.get(key, 0), 1 + int(round(4.0 * w)))
            if w >= 0.5:
                _T2_protected[key] = True
                _no_demote_until[key] = now + fresh_window
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
        else:
            ev_ts = _B2_ghost.get(key)
            age = (now - ev_ts) if isinstance(ev_ts, int) else (fresh_window + 1)
            _update_fresh_window_sample(age)
            w = max(0.0, 1.0 - float(age) / float(fresh_window))
            # ARC p toward frequency; amplify decrease during scans
            base_step = max(1.0, float(len(_B1_ghost)) / max(1.0, float(len(_B2_ghost))))
            step = base_step * (1.0 + 2.0 * w)
            if in_scan:
                step *= 1.2
            _adjust_p(-1, step, "ghost", now)
            _B2_ghost.pop(key, None)
            _freq[key] = max(_freq.get(key, 0), 1 + int(round(4.0 * w)))
            if w >= 0.5:
                _T2_protected[key] = True
                _no_demote_until[key] = now + fresh_window
                _demote_protected_if_needed(cache_snapshot, avoid_key=key)
            else:
                _T1_probation[key] = True
    else:
        # New key admission
        _T1_probation[key] = True
        if in_scan or (_last_victim_strength >= _VICTIM_GUARD_THRESH):
            _T1_probation.move_to_end(key, last=False)  # LRU side to dampen scan pollution
            if in_scan:
                # Non-ghost background nudge toward frequency under scans
                _adjust_p(-1, max(1.0, 0.1 * float(_cap_est)), "bg", now)
        else:
            _T1_probation.move_to_end(key, last=True)

    # De-dup across structures and trim ghosts
    if key in _T1_probation and key in _T2_protected:
        _T1_probation.pop(key, None)
    _B1_ghost.pop(key, None)
    _B2_ghost.pop(key, None)
    _ghost_trim()

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After eviction:
    - Move victim to its corresponding ghost with timestamp.
    - Track victim strength (decayed + T2 bonus) and set a short guard when a strong T2 victim is evicted.
    - Clean per-key state and maintain ghost bounds.
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
        _B1_ghost[key] = now

    # Clean per-key state
    m_key_timestamp.pop(key, None)
    _freq.pop(key, None)
    _refcnt.pop(key, None)
    _no_demote_until.pop(key, None)
    _first_touch_ts.pop(key, None)

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