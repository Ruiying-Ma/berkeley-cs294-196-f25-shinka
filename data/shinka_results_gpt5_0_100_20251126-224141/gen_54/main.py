# EVOLVE-BLOCK-START
"""S3Q Scan-Guarded SLRU with TinyLFU and Ghost-driven window adaptation

Resident segments:
- 'w': small window FIFO (recency burst absorption)
- 'p': probation (testing area)
- 'q': protected (frequent)

Ghost sets:
- Gp (m_ghost_b1_ts): keys evicted from recency side (w or p)
- Gq (m_ghost_b2_ts): keys evicted from protected side (q)

Adaptation:
- m_target_p is repurposed as the target window size (|W|), adaptively tuned via ghost hits.
- Asymmetric caps and ceiling ratios keep control responsive but stable.
- Scan guarding inserts brand-new keys into probation during cold streaks, protecting Q.
"""

# Per-key resident metadata
m_key_timestamp = dict()  # key -> last access time
m_key_segment = dict()    # key -> 'w' | 'p' | 'q'

# Ghost metadata (key -> last ghost timestamp)
m_ghost_b1_ts = dict()  # Gp: evicted from recency side (w or p)
m_ghost_b2_ts = dict()  # Gq: evicted from protected (q)

# Adaptive window target (repurposed from previous name p)
m_target_p = None  # desired size of |W|

# Scan and ghost tracking
m_last_ghost_hit_access = None
m_last_ghost_hit_side = None  # 'Gp' or 'Gq'
m_cold_streak = 0  # consecutive cold misses (not in ghosts)

# TinyLFU-like frequency sketch with periodic decay
m_freq = dict()              # key -> decaying frequency
m_next_decay_access = None   # access threshold for next decay


def _cap(cache_snapshot):
    try:
        return int(cache_snapshot.capacity)
    except Exception:
        # Fallback if capacity unknown; match number of cache entries
        return max(1, len(cache_snapshot.cache))


def _ensure_init(cache_snapshot):
    global m_target_p, m_last_ghost_hit_access, m_cold_streak, m_next_decay_access
    if m_target_p is None:
        # Start with a modest window (20% of capacity)
        m_target_p = max(1, _cap(cache_snapshot) // 5)
    if m_last_ghost_hit_access is None:
        m_last_ghost_hit_access = cache_snapshot.access_count
    if m_cold_streak is None:
        m_cold_streak = 0
    if m_next_decay_access is None:
        m_next_decay_access = cache_snapshot.access_count + max(8, _cap(cache_snapshot))


def _maybe_decay_freq(cache_snapshot):
    global m_freq, m_next_decay_access
    _ensure_init(cache_snapshot)
    if cache_snapshot.access_count >= (m_next_decay_access or 0):
        if m_freq:
            for k in list(m_freq.keys()):
                newc = m_freq.get(k, 0) >> 1
                if newc:
                    m_freq[k] = newc
                else:
                    m_freq.pop(k, None)
        m_next_decay_access = cache_snapshot.access_count + max(8, _cap(cache_snapshot))


def _bump_freq(key, w=1):
    try:
        inc = max(1, int(w))
    except Exception:
        inc = 1
    m_freq[key] = m_freq.get(key, 0) + inc


def _segment_keys(cache_snapshot):
    """Partition current residents by segment label."""
    w_keys, p_keys, q_keys = [], [], []
    for k in cache_snapshot.cache.keys():
        seg = m_key_segment.get(k)
        if seg == 'w':
            w_keys.append(k)
        elif seg == 'q':
            q_keys.append(k)
        else:
            # default unknown to probation for safety
            m_key_segment[k] = 'p'
            p_keys.append(k)
    return w_keys, p_keys, q_keys


def _lru_key(keys):
    if not keys:
        return None
    return min(keys, key=lambda k: m_key_timestamp.get(k, float('inf')))


def _choose_victim(keys):
    """Hybrid: prefer lowest freq, then oldest."""
    if not keys:
        return None
    return min(keys, key=lambda k: (m_freq.get(k, 0), m_key_timestamp.get(k, float('inf'))))


def _clamp_window_target(cap):
    """Clamp window size to a reasonable band."""
    global m_target_p
    min_w = max(1, cap // 64)
    max_w = max(1, cap // 2)
    if m_target_p < min_w:
        m_target_p = min_w
    elif m_target_p > max_w:
        m_target_p = max_w


def _prune_ghosts(cache_snapshot):
    """Keep total ghost size <= 2*cap; bias trimming opposite to last hit side."""
    global m_ghost_b1_ts, m_ghost_b2_ts
    cap = _cap(cache_snapshot)
    limit = max(1, 2 * cap)
    total = len(m_ghost_b1_ts) + len(m_ghost_b2_ts)
    while total > limit:
        # Prefer trimming the side opposite the last-hit side to retain the signal longer.
        if m_last_ghost_hit_side == 'Gp' and m_ghost_b2_ts:
            # trim oldest from Gq
            v = min(m_ghost_b2_ts, key=m_ghost_b2_ts.get)
            m_ghost_b2_ts.pop(v, None)
        elif m_last_ghost_hit_side == 'Gq' and m_ghost_b1_ts:
            # trim oldest from Gp
            v = min(m_ghost_b1_ts, key=m_ghost_b1_ts.get)
            m_ghost_b1_ts.pop(v, None)
        else:
            # Otherwise trim from the larger
            if len(m_ghost_b1_ts) >= len(m_ghost_b2_ts):
                if m_ghost_b1_ts:
                    v = min(m_ghost_b1_ts, key=m_ghost_b1_ts.get)
                    m_ghost_b1_ts.pop(v, None)
                elif m_ghost_b2_ts:
                    v = min(m_ghost_b2_ts, key=m_ghost_b2_ts.get)
                    m_ghost_b2_ts.pop(v, None)
            else:
                if m_ghost_b2_ts:
                    v = min(m_ghost_b2_ts, key=m_ghost_b2_ts.get)
                    m_ghost_b2_ts.pop(v, None)
                elif m_ghost_b1_ts:
                    v = min(m_ghost_b1_ts, key=m_ghost_b1_ts.get)
                    m_ghost_b1_ts.pop(v, None)
        total = len(m_ghost_b1_ts) + len(m_ghost_b2_ts)


def evict(cache_snapshot, obj):
    """
    Choose the eviction victim.
    Improvements:
    - Pre-REPLACE ghost-driven window adaptation so victim choice reflects newest signal.
    - Maintain segment budgets by demoting:
        * If Q > q_target (cap - |W| - 1), demote Q LRU -> P.
        * If W > |W| target, demote W LRU -> P (second-chance) instead of immediate eviction.
    - Ghost bias: if obj in Gq, favor evicting from recency side (P then W then Q).
                  if obj in Gp, favor evicting from Q first.
    Within any segment, choose by lowest frequency then LRU.
    """
    _ensure_init(cache_snapshot)
    cap = _cap(cache_snapshot)
    _clamp_window_target(cap)
    w_keys, p_keys, q_keys = _segment_keys(cache_snapshot)

    # Keep ghosts disjoint from current residents
    for k in cache_snapshot.cache.keys():
        m_ghost_b1_ts.pop(k, None)
        m_ghost_b2_ts.pop(k, None)

    # Pre-REPLACE: adjust window target based on ghost hits (ARC timing)
    in_gp = obj.key in m_ghost_b1_ts  # Gp (recency side)
    in_gq = obj.key in m_ghost_b2_ts  # Gq (protected)
    if in_gp or in_gq:
        inc_cap = max(1, cap // 8)
        dec_cap = max(1, (cap // 4) if m_cold_streak >= max(1, cap // 2) else (cap // 8))
        if in_gp:
            # Enlarge window target: ceil(|Gq|/|Gp|)
            denom = max(1, len(m_ghost_b1_ts))
            numer = len(m_ghost_b2_ts)
            raw_inc = max(1, (numer + denom - 1) // denom)
            m_target_p = min(cap, m_target_p + min(inc_cap, raw_inc))
            m_last_ghost_hit_access = cache_snapshot.access_count
            m_last_ghost_hit_side = 'Gp'
            m_cold_streak = 0
        else:
            # Shrink window target: ceil(|Gp|/|Gq|)
            denom = max(1, len(m_ghost_b2_ts))
            numer = len(m_ghost_b1_ts)
            raw_dec = max(1, (numer + denom - 1) // denom)
            m_target_p = max(0, m_target_p - min(dec_cap, raw_dec))
            m_last_ghost_hit_access = cache_snapshot.access_count
            m_last_ghost_hit_side = 'Gq'
            m_cold_streak = 0
        _clamp_window_target(cap)

    # Re-evaluate segments after potential resizes (keys unchanged, but targets changed)
    w_target = max(1, m_target_p)
    q_target = max(1, cap - w_target - 1)  # leave at least one slot for probation

    # Demote Q overflow to P (at most a couple per call to bound work)
    if len(q_keys) > q_target:
        excess = len(q_keys) - q_target
        # demote up to 2 keys to avoid heavy work
        for _ in range(min(excess, 2)):
            k = _lru_key(q_keys)
            if k is None:
                break
            m_key_segment[k] = 'p'
            # update local lists
            q_keys.remove(k)
            p_keys.append(k)

    # Demote W overflow to P (second-chance) instead of direct eviction
    if len(w_keys) > w_target:
        excess = len(w_keys) - w_target
        for _ in range(min(excess, 2)):
            k = _lru_key(w_keys)
            if k is None:
                break
            m_key_segment[k] = 'p'
            w_keys.remove(k)
            p_keys.append(k)

    # Ghost-biased replacement choice using refreshed lists
    if in_gq:
        # Favor recency side first: P -> W -> Q
        victim = _choose_victim(p_keys) or _choose_victim(w_keys) or _choose_victim(q_keys)
        if victim is not None:
            return victim
    elif in_gp:
        # Favor protected first: Q -> P -> W
        victim = _choose_victim(q_keys) or _choose_victim(p_keys) or _choose_victim(w_keys)
        if victim is not None:
            return victim

    # Default SLRU: P -> W -> Q
    victim = _choose_victim(p_keys)
    if victim is not None:
        return victim
    victim = _choose_victim(w_keys) or _choose_victim(q_keys)
    if victim is not None:
        return victim

    # Last resort: global choice
    all_keys = list(cache_snapshot.cache.keys())
    return _choose_victim(all_keys) if all_keys else None


def update_after_hit(cache_snapshot, obj):
    """
    On hit:
    - Refresh timestamp and bump frequency.
    - Move from W or P to Q (promotion).
    - Light idle drift: if no ghost hits for ~cap accesses, nudge window toward baseline.
    - Keep ghosts disjoint.
    """
    global m_cold_streak, m_target_p, m_last_ghost_hit_access
    _ensure_init(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    now = cache_snapshot.access_count
    cap = _cap(cache_snapshot)
    base_w = max(1, cap // 5)

    # Reset cold streak and update stats
    m_cold_streak = 0
    m_key_timestamp[obj.key] = now
    _bump_freq(obj.key, 2)

    # Promotion to protected
    seg = m_key_segment.get(obj.key, 'p')
    if seg in ('w', 'p'):
        m_key_segment[obj.key] = 'q'

    # Idle drift for window target (slowly home toward baseline without ghost signals)
    if now - m_last_ghost_hit_access > cap:
        if m_target_p > base_w:
            m_target_p -= 1
        elif m_target_p < base_w:
            m_target_p += 1
        _clamp_window_target(cap)

    # Keep ghosts disjoint
    m_ghost_b1_ts.pop(obj.key, None)
    m_ghost_b2_ts.pop(obj.key, None)


def update_after_insert(cache_snapshot, obj):
    """
    On insert (miss path):
    - Pre-REPLACE adaptation is handled in evict; here we only control placement and bookkeeping.
    - Scan-guard insertion: during sustained cold streaks, insert into probation P
      instead of window W.
    - On ghost hits, promote to Q immediately and reset cold streak; do not adjust window here.
    """
    global m_last_ghost_hit_access, m_last_ghost_hit_side, m_cold_streak
    _ensure_init(cache_snapshot)
    _maybe_decay_freq(cache_snapshot)

    cap = _cap(cache_snapshot)
    _clamp_window_target(cap)

    in_gp = obj.key in m_ghost_b1_ts
    in_gq = obj.key in m_ghost_b2_ts

    seg = 'w'  # default insertion into window

    if in_gp:
        # Strong recency signal: protect immediately
        seg = 'q'
        m_ghost_b1_ts.pop(obj.key, None)
        m_ghost_b2_ts.pop(obj.key, None)
        m_last_ghost_hit_access = cache_snapshot.access_count
        m_last_ghost_hit_side = 'Gp'
        m_cold_streak = 0
        _bump_freq(obj.key, 3)
    elif in_gq:
        # Strong frequency signal: protect immediately
        seg = 'q'
        m_ghost_b2_ts.pop(obj.key, None)
        m_ghost_b1_ts.pop(obj.key, None)
        m_last_ghost_hit_access = cache_snapshot.access_count
        m_last_ghost_hit_side = 'Gq'
        m_cold_streak = 0
        _bump_freq(obj.key, 4)
    else:
        # Cold miss: scan guard decides placement
        m_cold_streak += 1
        if m_cold_streak >= max(1, cap // 2):
            seg = 'p'
        else:
            seg = 'w'
        _bump_freq(obj.key, 1)

    # Install resident metadata
    m_key_segment[obj.key] = seg
    m_key_timestamp[obj.key] = cache_snapshot.access_count

    # Ensure ghosts remain disjoint
    m_ghost_b1_ts.pop(obj.key, None)
    m_ghost_b2_ts.pop(obj.key, None)

    # Control ghost history size
    _prune_ghosts(cache_snapshot)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    After eviction, move evicted resident to appropriate ghost:
    - If evicted from Q → Gq
    - Else (from W or P) → Gp
    """
    _ensure_init(cache_snapshot)

    seg = m_key_segment.pop(evicted_obj.key, 'p')
    m_key_timestamp.pop(evicted_obj.key, None)

    ts = cache_snapshot.access_count
    if seg == 'q':
        m_ghost_b2_ts[evicted_obj.key] = ts  # Gq
        m_ghost_b1_ts.pop(evicted_obj.key, None)
    else:
        m_ghost_b1_ts[evicted_obj.key] = ts  # Gp
        m_ghost_b2_ts.pop(evicted_obj.key, None)

    _prune_ghosts(cache_snapshot)
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