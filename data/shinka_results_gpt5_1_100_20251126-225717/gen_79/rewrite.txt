# EVOLVE-BLOCK-START
"""LeCaR + TinyLFU mixture: learned blend of LRU and LFU with ghost-based feedback and scan resistance."""

from collections import OrderedDict

# Global recency structure (single list for all cached keys)
_lru = OrderedDict()  # key -> None (MRU at end, LRU at front)

# Decayed TinyLFU-like frequency counter: key -> (count, epoch_when_last_updated)
_freq = {}
_epoch = 0
_last_epoch_tick = 0
_DECAY_WINDOW = 128  # accesses per epoch; tied to capacity adaptively

# Ghost histories: which expert last evicted the key, for feedback on re-reference
# - _GLRU: keys evicted when LRU expert was chosen
# - _GLFU: keys evicted when LFU expert was chosen
_GLRU = OrderedDict()  # key -> epoch of ghost insertion
_GLFU = OrderedDict()  # key -> epoch of ghost insertion
_GHOST_LIMIT_MULT = 2

# Expert weights (normalized continuously)
_w_lru = 0.5
_w_lfu = 0.5
_LR = 0.08  # learning rate for weight updates

# Last eviction bookkeeping
_last_victim_score = 0
_victim_policy = {}  # key -> 'LRU' or 'LFU' used for its eviction

# Scan detection
_scan_mode = False
_epoch_unique = set()

# Freshness window derived from ghost reuse (for weighting expert updates)
_fresh_epoch_win = 2


# ---------------- Helpers ----------------
def _ensure_params(cache_snapshot):
    global _DECAY_WINDOW
    cap = max(1, cache_snapshot.capacity)
    base = max(64, cap)
    # Slightly faster aging when scan mode is engaged
    _DECAY_WINDOW = max(32, base // 2) if _scan_mode else base


def _median(vals):
    if not vals:
        return 0
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) // 2


def _recompute_fresh_epoch_window(cache_snapshot):
    """Adaptive freshness window (in epochs) from ghost ages."""
    global _fresh_epoch_win
    ages = []
    for e in _GLRU.values():
        ages.append(max(0, _epoch - e))
    for e in _GLFU.values():
        ages.append(max(0, _epoch - e))
    if not ages:
        _fresh_epoch_win = max(1, _fresh_epoch_win)
        return
    med = max(1, _median(ages))
    cap = max(1, cache_snapshot.capacity)
    cap_epochs = max(1, int(round(cap / float(max(1, _DECAY_WINDOW)))))
    lower = max(1, int(0.25 * cap_epochs))
    upper = max(lower, cap_epochs)
    _fresh_epoch_win = max(lower, min(med, upper))


def _maybe_age(cache_snapshot):
    """Epoch handling, scan detection, and ghost trimming."""
    global _epoch, _last_epoch_tick, _scan_mode
    _ensure_params(cache_snapshot)
    if cache_snapshot.access_count - _last_epoch_tick >= _DECAY_WINDOW:
        window = max(1, _DECAY_WINDOW)
        unique_density = min(1.0, len(_epoch_unique) / float(window))
        hit_rate = cache_snapshot.hit_count / max(1.0, float(cache_snapshot.access_count))
        _scan_mode = (unique_density > 0.7 and hit_rate < 0.25)

        _epoch += 1
        _last_epoch_tick = cache_snapshot.access_count
        _epoch_unique.clear()

        # Trim ghosts
        limit = max(1, _GHOST_LIMIT_MULT * max(1, cache_snapshot.capacity))
        while len(_GLRU) > limit:
            _GLRU.popitem(last=False)
        while len(_GLFU) > limit:
            _GLFU.popitem(last=False)

        # Recompute freshness window for expert update scaling
        _recompute_fresh_epoch_window(cache_snapshot)

        # Soft normalization of expert weights to prevent drift
        _normalize_weights()


def _normalize_weights():
    global _w_lru, _w_lfu
    # Keep weights bounded and normalized
    _w_lru = max(1e-6, _w_lru)
    _w_lfu = max(1e-6, _w_lfu)
    s = _w_lru + _w_lfu
    _w_lru /= s
    _w_lfu /= s


def _score(key):
    """Decayed TinyLFU score for a key."""
    ce = _freq.get(key)
    if ce is None:
        return 0
    c, e = ce
    de = _epoch - e
    if de > 0:
        c = c >> min(6, de)  # halve per epoch (up to 64x)
    return max(0, c)


def _inc(key, by=1):
    """Increase decayed frequency, decaying if epoch advanced."""
    c, e = _freq.get(key, (0, _epoch))
    if e != _epoch:
        c = c >> min(6, _epoch - e)
        e = _epoch
    c = min(c + max(1, by), 1 << 30)
    _freq[key] = (c, e)


def _sync_metadata(cache_snapshot):
    """Keep LRU in sync with the actual cache content."""
    cached = set(cache_snapshot.cache.keys())
    # Remove stale
    for k in list(_lru.keys()):
        if k not in cached:
            _lru.pop(k, None)
    # Add missing (place at MRU)
    for k in cached:
        if k not in _lru:
            _lru[k] = None


def _lru_candidate():
    """Return the LRU-side victim candidate (oldest)."""
    try:
        k, _ = next(iter(_lru.items()))
        return k
    except StopIteration:
        return None


def _sample_keys(od, n_front, n_back):
    """Collect up to n_front keys from LRU side and n_back from MRU side."""
    res = []
    it = iter(od.keys())
    for _ in range(max(0, n_front)):
        try:
            res.append(next(it))
        except StopIteration:
            break
    if n_back > 0 and od:
        # Walk from MRU end
        rit = reversed(od.keys())
        for _ in range(n_back):
            try:
                k = next(rit)
                if k not in res:
                    res.append(k)
            except StopIteration:
                break
    return res


def _lfu_candidate():
    """Return a low-frequency candidate using a small biased sample."""
    if not _lru:
        return None
    # Sample more from older side, some from MRU side to catch very cold recency
    n = len(_lru)
    n_front = 4 if n <= 32 else 6 if n <= 128 else 8
    n_back = 1 if n <= 64 else 2
    best = None
    best_tuple = None  # (score, age_bias, rec_idx)
    # rec_idx: order from LRU side for tie-break
    keys = _sample_keys(_lru, n_front, n_back)
    # Precompute recency rank for LRU-sampled keys
    front_keys = list(_sample_keys(_lru, n_front, 0))
    rec_index = {k: i for i, k in enumerate(front_keys)}
    for k in keys:
        s = _score(k)
        # Age bias: older keys more eligible on ties
        ci = rec_index.get(k, n_front + 1)
        tup = (s, ci, k)
        if best_tuple is None or tup < best_tuple:
            best_tuple = tup
            best = k
    # Fallback to LRU if nothing found
    return best if best is not None else _lru_candidate()


def _choose_policy():
    """Choose expert deterministically by higher weight."""
    return 'LRU' if _w_lru >= _w_lfu else 'LFU'


def _update_weights_for_reuse(reused_policy, age_epochs):
    """Penalize the evicting expert when a ghost key is re-referenced; reward the other.

    reused_policy: 'LRU' or 'LFU' – which expert evicted this key previously.
    age_epochs: how many epochs since eviction (smaller means stronger signal).
    """
    global _w_lru, _w_lfu
    fresh_win = max(1, int(_fresh_epoch_win))
    # Freshness weight w in [0,1], stronger when reuse sooner than fresh_win
    w = max(0.0, 1.0 - (age_epochs / float(fresh_win)))
    step = _LR * (1.0 + 2.0 * w)
    if reused_policy == 'LRU':
            # LRU made a mistake; shift weight toward LFU
            _w_lru *= (1.0 - step)
            _w_lfu *= (1.0 + 0.5 * step)
    else:
            _w_lfu *= (1.0 - step)
            _w_lru *= (1.0 + 0.5 * step)
    _normalize_weights()


def _admit_to_mru(cache_snapshot, k):
    """Place key at MRU in global LRU."""
    # Remove any stale positions then append MRU
    _lru.pop(k, None)
    _lru[k] = None


def _admit_to_lru(cache_snapshot, k):
    """Place key at LRU side to deprioritize (pollution control)."""
    _lru.pop(k, None)
    _lru[k] = None
    # Move immediately to LRU side (front)
    try:
        first = next(iter(_lru.keys()))
        if first != k:
            # rotate: move k to front by popping others then reinserting; OrderedDict has move_to_end only
            # Trick: move all except k to end, then move k to front by re-insertion
            _lru.move_to_end(k, last=False)
        else:
            # already at front
            pass
    except StopIteration:
        pass


# ---------------- Core API ----------------
def evict(cache_snapshot, obj):
    """
    Choose an eviction victim key to make space for obj using a learned blend of LRU/LFU.
    """
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)

    # Track unique for scan detection
    try:
        _epoch_unique.add(obj.key)
    except Exception:
        pass

    if not cache_snapshot.cache:
        return None

    # During scans, evict strict LRU to protect against pollution
    if _scan_mode:
        k = _lru_candidate()
        return k if k is not None else next(iter(cache_snapshot.cache.keys()))

    # Get candidates from both experts
    k_lru = _lru_candidate()
    k_lfu = _lfu_candidate()

    # If one candidate missing, fall back to the other
    if k_lru is None and k_lfu is None:
        return next(iter(cache_snapshot.cache.keys()))
    if k_lru is None:
        chosen = k_lfu
        _victim_policy[chosen] = 'LFU'
        return chosen
    if k_lfu is None:
        chosen = k_lru
        _victim_policy[chosen] = 'LRU'
        return chosen

    # Admission-aware tie-break: if the incoming item is hotter than one candidate, prefer evicting the colder
    incoming_score = _score(obj.key)
    s_lru = _score(k_lru)
    s_lfu = _score(k_lfu)

    # Policy choice by weight; if both candidates exist, optionally overrule by clear coldness gap
    policy = _choose_policy()

    # If one candidate is clearly colder (by score and older recency), choose it regardless of policy
    if (s_lru + (1 if k_lru != k_lfu else 0)) < s_lfu - 1:
        policy = 'LRU'
    elif (s_lfu + (1 if k_lru != k_lfu else 0)) < s_lru - 1:
        policy = 'LFU'
    else:
        # Further bias to avoid evicting a key hotter than the incoming object when possible
        if s_lru > incoming_score and s_lfu <= incoming_score:
            policy = 'LFU'
        elif s_lfu > incoming_score and s_lru <= incoming_score:
            policy = 'LRU'

    chosen = k_lru if policy == 'LRU' else k_lfu
    _victim_policy[chosen] = policy
    return chosen


def update_after_hit(cache_snapshot, obj):
    """
    Update metadata after a cache hit: refresh recency and bump TinyLFU.
    """
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)

    k = obj.key
    _inc(k)
    try:
        _epoch_unique.add(k)
    except Exception:
        pass

    # Refresh to MRU
    if k in _lru:
        _lru.move_to_end(k, last=True)
    else:
        _admit_to_mru(cache_snapshot, k)

    # No expert update on hits; learning happens on re-reference after eviction.
    # However, small nudge: when we see many hits on young items, favor LRU slightly.
    # When hits occur on older items (low score but survived), LFU gets a tiny boost.
    s = _score(k)
    try:
        rec_pos = 0
        # Approximate recency rank by distance from MRU end: if near end, it's recent
        # We can't easily compute exact index; skip precise rank to keep overhead minimal.
        recent = True  # treat hits as recent by default
        global _w_lru, _w_lfu
        if recent and s <= 1:
            _w_lru *= (1.0 + 0.005)
        elif s >= 3:
            _w_lfu *= (1.0 + 0.005)
        _normalize_weights()
    except Exception:
        pass


def update_after_insert(cache_snapshot, obj):
    """
    Update metadata right after inserting a new object: admission placement and ghost-informed boosting.
    """
    _maybe_age(cache_snapshot)
    _sync_metadata(cache_snapshot)

    k = obj.key
    _inc(k)
    try:
        _epoch_unique.add(k)
    except Exception:
        pass

    # Use ghost information for readmission
    g_lru = _GLRU.get(k)
    g_lfu = _GLFU.get(k)
    age_lru = (_epoch - g_lru) if g_lru is not None else None
    age_lfu = (_epoch - g_lfu) if g_lfu is not None else None
    fresh_win = max(1, int(_fresh_epoch_win))
    w_lru = max(0.0, 1.0 - (float(age_lru) / float(fresh_win))) if age_lru is not None else 0.0
    w_lfu = max(0.0, 1.0 - (float(age_lfu) / float(fresh_win))) if age_lfu is not None else 0.0

    # Basic admission guard: compare incoming score with last victim; if colder, place at LRU
    incoming_score = _score(k)
    colder_than_victim = incoming_score < _last_victim_score

    if _scan_mode:
        # During scans: place new entries at LRU side to get evicted quickly, learning steers toward LRU
        _admit_to_lru(cache_snapshot, k)
    else:
        if g_lfu is not None and w_lfu >= 0.5:
            # Recently evicted by LFU but reappeared soon -> it was a recency-friendly object
            _admit_to_mru(cache_snapshot, k)
            # Reward LRU slightly
            global _w_lru
            _w_lru *= (1.0 + 0.02)
            _normalize_weights()
        elif g_lru is not None and w_lru >= 0.5:
            # Recently evicted by LRU but reappeared soon -> it was a frequency-friendly object
            _admit_to_mru(cache_snapshot, k)
            global _w_lfu
            _w_lfu *= (1.0 + 0.02)
            _normalize_weights()
        else:
            # No strong ghost signal: if colder than victim, place at LRU; otherwise MRU
            if colder_than_victim and incoming_score == 0:
                _admit_to_lru(cache_snapshot, k)
            else:
                _admit_to_mru(cache_snapshot, k)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    Update metadata after evicting a victim: record ghost and learn from re-references.
    """
    _maybe_age(cache_snapshot)

    victim_key = evicted_obj.key

    # Remove from LRU if present
    _lru.pop(victim_key, None)

    # Record last victim score for admission guard
    global _last_victim_score
    _last_victim_score = _score(victim_key)

    # Determine which policy evicted it
    policy = _victim_policy.pop(victim_key, 'LRU')
    if policy == 'LRU':
        _GLRU[victim_key] = _epoch
        _GLFU.pop(victim_key, None)
    else:
        _GLFU[victim_key] = _epoch
        _GLRU.pop(victim_key, None)

    # Trim ghosts to bounded size
    limit = max(1, _GHOST_LIMIT_MULT * max(1, cache_snapshot.capacity))
    while len(_GLRU) > limit:
        _GLRU.popitem(last=False)
    while len(_GLFU) > limit:
        _GLFU.popitem(last=False)

    # If incoming object was in ghosts, update expert weights (LeCaR-style feedback)
    try:
        key_in = obj.key
        if key_in in _GLRU:
            age = max(0, _epoch - _GLRU.get(key_in, _epoch))
            # LRU evicted this previously; it came back -> penalize LRU, reward LFU
            _update_weights_for_reuse('LRU', age)
        elif key_in in _GLFU:
            age = max(0, _epoch - _GLFU.get(key_in, _epoch))
            # LFU evicted this previously; it came back -> penalize LFU, reward LRU
            _update_weights_for_reuse('LFU', age)
    except Exception:
        pass

    # During scans, also bias weights slightly toward LRU to respond quicker
    if _scan_mode:
        global _w_lru, _w_lfu
        _w_lru *= 1.02
        _w_lfu *= 0.98
        _normalize_weights()
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