# EVOLVE-BLOCK-START
"""W-TinyLFU + SLRU: windowed TinyLFU admission with probation/protected segments."""

from collections import OrderedDict
import math

# Resident segments (LRU->MRU order)
_WIN = OrderedDict()    # small recency window
_PROB = OrderedDict()   # SLRU probation
_PROT = OrderedDict()   # SLRU protected

# Segment index
_SEG = {}               # key -> 'win' | 'prob' | 'prot'

# Timestamps for tie-breaking
_TS = {}                # key -> last access_count

# Count-Min Sketch (TinyLFU)
_CMS_D = 4
_CMS_W = 0
_CMS = []               # list of D arrays (length W)
_CMS_MASK = 0           # if W is power of two, mask = W-1
_LAST_DECAY_TICK = 0
_DECAY_INTERVAL = 0

# Parameters
_WIN_FRAC = 0.10        # 10% window
_PROT_FRAC = 0.80       # of (total - window)
_MIN_WIN = 0.05
_MAX_WIN = 0.25
_SAMPLE_PROB = 4        # probation tail sample for eviction decision

# EWMA for scan sensing (soft biasing only)
_HIT_EWMA = 0.0
_INS_EWMA = 0.0
_ALPHA = 0.05
_SCAN = False

# Cache capacity estimate
_CAP = 0

def _ensure(cache_snapshot):
    """Initialize capacity and CMS structures."""
    global _CAP, _CMS_W, _CMS, _CMS_MASK, _DECAY_INTERVAL
    cap = getattr(cache_snapshot, "capacity", None)
    if isinstance(cap, int) and cap > 0:
        _CAP = cap
    else:
        _CAP = max(_CAP, len(cache_snapshot.cache))
    if _CAP <= 0:
        _CAP = max(1, len(cache_snapshot.cache))
    # Set CMS width to next power-of-two >= 4*cap (bounded)
    target = max(64, 4 * _CAP)
    pow2 = 1 << (target - 1).bit_length()
    if _CMS_W != pow2 or not _CMS:
        _CMS_W = pow2
        _CMS_MASK = _CMS_W - 1
        _CMS[:] = [[0] * _CMS_W for _ in range(_CMS_D)]
    # Decay roughly every capacity accesses
    _DECAY_INTERVAL = max(32, _CAP)

def _h(key, i):
    # Derive stable-ish integer from key and i; hash is randomized per process but fine for our purposes
    return hash(f"{i}|{key}") & _CMS_MASK

def _cms_inc(key):
    for i in range(_CMS_D):
        idx = _h(key, i)
        v = _CMS[i][idx]
        # saturate softly
        _CMS[i][idx] = v + 1 if v < (1 << 31) - 1 else v

def _cms_est(key):
    if _CMS_W == 0:
        return 0
    m = None
    for i in range(_CMS_D):
        idx = _h(key, i)
        v = _CMS[i][idx]
        m = v if m is None else (v if v < m else m)
    return 0 if m is None else m

def _maybe_decay_counts(cache_snapshot):
    global _LAST_DECAY_TICK
    now = cache_snapshot.access_count
    if now - _LAST_DECAY_TICK >= _DECAY_INTERVAL and _CMS:
        # Right shift all counters by 1
        for i in range(_CMS_D):
            arr = _CMS[i]
            # simple loop to avoid importing numpy
            for j in range(_CMS_W):
                arr[j] >>= 1
        _LAST_DECAY_TICK = now

def _update_activity(is_hit, cache_snapshot):
    global _HIT_EWMA, _INS_EWMA, _ALPHA, _SCAN
    alpha = _ALPHA
    _HIT_EWMA = (1.0 - alpha) * _HIT_EWMA + (alpha if is_hit else 0.0)
    _INS_EWMA = (1.0 - alpha) * _INS_EWMA + (0.0 if is_hit else alpha)
    # Simple scan heuristic: many misses and low hit rate
    _SCAN = (_INS_EWMA > 0.7) and (_HIT_EWMA < 0.2)

def _seg_place(key, seg, mru=True):
    # Remove from any segment, then place
    if key in _WIN: _WIN.pop(key, None)
    if key in _PROB: _PROB.pop(key, None)
    if key in _PROT: _PROT.pop(key, None)
    if seg == 'win':
        _WIN[key] = True
        if not mru:
            _WIN.move_to_end(key, last=False)
    elif seg == 'prob':
        _PROB[key] = True
        if not mru:
            _PROB.move_to_end(key, last=False)
    else:
        _PROT[key] = True
    _SEG[key] = seg

def _rebalance_after_promotion(cache_snapshot):
    """Ensure protected isn't oversized vs target by demoting its LRU to probation."""
    tot = len(cache_snapshot.cache)
    if tot <= 0:
        return
    win_t = max(1, int(tot * _WIN_FRAC))
    main_t = max(0, tot - win_t)
    prot_t = max(0, int(main_t * _PROT_FRAC))
    # Demote if protected too large
    while len(_PROT) > prot_t and len(_PROT) > 0:
        # demote LRU of protected into probation MRU
        for k in _PROT.keys():
            _PROT.pop(k, None)
            _SEG[k] = 'prob'
            _PROB[k] = True
            break

def _lru_key(od):
    for k in od.keys():
        return k
    return None

def _sample_probation(n, cache_snapshot):
    res = []
    it = iter(_PROB.keys())
    for _ in range(n):
        try:
            k = next(it)
        except StopIteration:
            break
        if k in cache_snapshot.cache:
            res.append(k)
    return res

def _targets(cache_snapshot):
    tot = len(cache_snapshot.cache)
    if tot <= 0:
        return (0, 0)
    win_t = max(1, int(tot * _WIN_FRAC))
    main_t = max(0, tot - win_t)
    prot_t = max(0, int(main_t * _PROT_FRAC))
    return (win_t, prot_t)

def evict(cache_snapshot, obj):
    """
    Admission-by-comparison:
    - Estimate incoming popularity using TinyLFU (CMS).
    - Compare to a few LRU-side probation candidates.
    - If incoming is colder, evict from the window; else evict from probation.
    - If one segment is empty, fall back to the other; protected used only if both are empty.
    """
    _ensure(cache_snapshot)
    _maybe_decay_counts(cache_snapshot)
    incoming_key = obj.key if obj is not None else None
    f_in = _cms_est(incoming_key) if incoming_key is not None else 0

    # Bias under scans: prefer evicting from window to protect main working set
    if _SCAN and _WIN:
        k = _lru_key(_WIN)
        if k in cache_snapshot.cache:
            return k

    # Prepare candidates
    prob_cands = _sample_probation(_SAMPLE_PROB if (_SAMPLE_PROB := _SAMPLE_PROB) else 4, cache_snapshot)  # localize name
    # pick the coldest probation candidate by CMS; tie-break by recency
    prob_victim = None
    prob_score = None
    for k in prob_cands:
        s = _cms_est(k)
        t = _TS.get(k, 0)
        tup = (s, t)
        if prob_score is None or tup < prob_score:
            prob_score = tup
            prob_victim = k

    win_victim = _lru_key(_WIN) if _WIN else None

    # If either segment missing, fallback
    if win_victim is None and prob_victim is None:
        # both empty: use protected LRU
        k = _lru_key(_PROT)
        if k is not None and k in cache_snapshot.cache:
            return k
        # fallback to global LRU
        keys = list(cache_snapshot.cache.keys())
        return keys[0] if keys else None
    if win_victim is None:
        return prob_victim
    if prob_victim is None:
        return win_victim

    # Compare incoming vs probation candidate
    f_prob = prob_score[0] if prob_score is not None else 0
    # small slack to avoid thrashing protected/probation on ties; stronger under poor locality
    slack = 1 if _HIT_EWMA < 0.3 else 0
    if f_in + slack <= f_prob:
        # incoming is colder -> evict from window
        return win_victim
    else:
        # incoming hotter -> evict from probation
        return prob_victim

def update_after_hit(cache_snapshot, obj):
    """
    On hit:
    - Update CMS and timestamp.
    - If in window: two-touch promotion to protected.
    - If in probation: promote to protected.
    - If in protected: refresh MRU.
    - Demote protected if it exceeds target size.
    """
    _ensure(cache_snapshot)
    _maybe_decay_counts(cache_snapshot)
    _update_activity(True, cache_snapshot)

    k = obj.key
    now = cache_snapshot.access_count
    _TS[k] = now
    _cms_inc(k)

    seg = _SEG.get(k)
    if seg == 'win':
        # Two-touch: promote on hit
        _seg_place(k, 'prot', mru=True)
    elif seg == 'prob':
        # Probation -> Protected on hit
        _seg_place(k, 'prot', mru=True)
    elif seg == 'prot':
        # Refresh MRU
        if k in _PROT:
            _PROT.move_to_end(k, last=True)
        else:
            _seg_place(k, 'prot', mru=True)
    else:
        # Unknown metadata (rare): place to protected as it's hot
        _seg_place(k, 'prot', mru=True)

    _rebalance_after_promotion(cache_snapshot)

def update_after_insert(cache_snapshot, obj):
    """
    On miss + insert:
    - Update CMS and timestamp.
    - Place into window MRU by default.
    - Under heavy scan, bias to probation LRU to reduce window churn.
    """
    _ensure(cache_snapshot)
    _maybe_decay_counts(cache_snapshot)
    _update_activity(False, cache_snapshot)

    k = obj.key
    now = cache_snapshot.access_count
    _TS[k] = now
    _cms_inc(k)

    # Insert policy
    if _SCAN:
        # Put into probation LRU so that future evictions prefer window first
        _seg_place(k, 'prob', mru=False)
    else:
        _seg_place(k, 'win', mru=True)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    After eviction:
    - Remove victim from its resident segment and metadata.
    """
    _ensure(cache_snapshot)
    vic = evicted_obj.key

    # Remove from segment
    if _SEG.get(vic) == 'win':
        _WIN.pop(vic, None)
    elif _SEG.get(vic) == 'prob':
        _PROB.pop(vic, None)
    elif _SEG.get(vic) == 'prot':
        _PROT.pop(vic, None)
    _SEG.pop(vic, None)

    # Clean timestamp; CMS kept (TinyLFU relies on history)
    _TS.pop(vic, None)

    # Optional: small adaptive window tweak based on which segment lost
    # If we had to evict protected (rare), slightly lower protected share
    try:
        tot = len(cache_snapshot.cache)
        if tot > 0:
            win_t, prot_t = _targets(cache_snapshot)
            # If protected size is 0 but probation large, nudge window slightly up to absorb scans
            if len(_PROT) == 0 and len(_PROB) > win_t:
                global _WIN_FRAC
                _WIN_FRAC = min(_MAX_WIN, _WIN_FRAC + 0.01)
    except Exception:
        pass
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