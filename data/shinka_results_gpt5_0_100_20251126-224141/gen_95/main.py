# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict
import math

# LRU timestamp map kept for tie-breaking and fallback
m_key_timestamp = dict()

# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1 (recent history)
arc_B2 = OrderedDict()  # ghost of T2 (frequent history)
arc_p = 0               # target size of T1
arc_capacity = None     # initialized from cache_snapshot

# Idle tracking and scan handling
last_ghost_hit_access = -1  # last access_count when B1/B2 was hit
cold_streak = 0             # consecutive brand-new inserts (no ghost)
scan_guard_until = -1       # guard window end
cold_extra_applied = False  # one-time extra clamp during cold scans
guard_demote_once = False   # one-shot demotion bias flag
# Per-access flag indicating whether p was already adjusted on a ghost reference
p_adjusted_this_access = False

# Track which list the eviction candidate was chosen from to ensure correct ghosting
last_replaced_from = None   # 'T1' or 'T2'


def _ensure_capacity(cache_snapshot):
    global arc_capacity
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)


def _move_to_mru(od, key):
    # Push key to MRU position of an OrderedDict
    if key in od:
        od.pop(key, None)
    od[key] = True


def _insert_at_lru(od, key):
    # Insert key at LRU position (probation)
    if key in od:
        od.pop(key, None)
    od[key] = True
    try:
        # Move to beginning (LRU side)
        od.move_to_end(key, last=False)
    except Exception:
        pass


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _guard_window(C):
    # Short, gentle guard window length
    return min(8, max(1, C // 16))


def _trim_ghosts():
    # Keep ghosts total size within capacity with p-aware hysteresis
    total = len(arc_B1) + len(arc_B2)
    C = arc_capacity if arc_capacity is not None else 1
    target_B1 = min(C, max(0, arc_p))
    target_B2 = max(0, C - target_B1)
    h = max(1, C // 32)  # hysteresis to reduce oscillation
    while total > C:
        over_B1 = len(arc_B1) - target_B1
        over_B2 = len(arc_B2) - target_B2
        if over_B1 > h and arc_B1:
            _pop_lru(arc_B1)
        elif over_B2 > h and arc_B2:
            _pop_lru(arc_B2)
        else:
            # Otherwise trim from the larger side
            if len(arc_B1) >= len(arc_B2):
                _pop_lru(arc_B1)
            else:
                _pop_lru(arc_B2)
        total = len(arc_B1) + len(arc_B2)


def _resync(cache_snapshot):
    # Ensure resident metadata tracks actual cache content
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Any cached keys not tracked: seed using ghost hints for better accuracy
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            if k in arc_B2:
                _move_to_mru(arc_T2, k)
                arc_B2.pop(k, None)
            elif k in arc_B1:
                _move_to_mru(arc_T1, k)
                arc_B1.pop(k, None)
            else:
                _move_to_mru(arc_T1, k)
    # Keep ghosts disjoint from residents (robustness)
    for k in list(arc_B1.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B1.pop(k, None)
    for k in list(arc_B2.keys()):
        if k in arc_T1 or k in arc_T2:
            arc_B2.pop(k, None)
    _trim_ghosts()


def _decay_p_if_idle(cache_snapshot):
    # Proportional, bounded decay of p when no ghost hits for a while; plus one-shot cold clamp
    global arc_p, cold_extra_applied
    C = arc_capacity if arc_capacity else 1
    if last_ghost_hit_access >= 0 and arc_p > 0:
        idle = cache_snapshot.access_count - last_ghost_hit_access
        if idle > 0:
            cap_step = max(1, C // 8)
            dyn_step = max(1, idle // max(1, C // 4))
            step = min(cap_step, dyn_step)
            arc_p = max(0, arc_p - step)
    # One-time extra clamp during prolonged cold streaks (scan-like) to accelerate recovery
    if cold_streak >= max(1, C // 2) and not cold_extra_applied:
        extra = min(max(1, C // 4), max(1, cold_streak // max(1, C // 8)))
        arc_p = max(0, arc_p - extra)
        cold_extra_applied = True


def evict(cache_snapshot, obj):
    '''
    Choose the eviction victim.
    - Return: candid_obj_key
    '''
    global arc_p, last_ghost_hit_access, cold_streak, scan_guard_until, cold_extra_applied, last_replaced_from, guard_demote_once, p_adjusted_this_access
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)
    p_adjusted_this_access = False

    key = obj.key
    C = arc_capacity if arc_capacity else 1
    in_B1 = key in arc_B1
    in_B2 = key in arc_B2

    # Canonical ghost-driven p updates BEFORE REPLACE (ARC)
    if in_B1:
        # step_up = ceil(|B2|/|B1|); clamp by C//8
        denom = max(1, len(arc_B1))
        step_up = (len(arc_B2) + denom - 1) // denom
        arc_p = min(C, arc_p + min(step_up, max(1, C // 8)))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        scan_guard_until = -1
        guard_demote_once = False
        cold_extra_applied = False
        p_adjusted_this_access = True
    elif in_B2:
        # step_down = ceil(|B1|/|B2|); clamp by C//8 (or C//4 under prolonged cold streaks)
        denom = max(1, len(arc_B2))
        step_down = (len(arc_B1) + denom - 1) // denom
        dec_cap = max(1, (C // 4) if cold_streak >= max(1, C // 2) else (C // 8))
        arc_p = max(0, arc_p - min(step_down, dec_cap))
        last_ghost_hit_access = cache_snapshot.access_count
        cold_streak = 0
        scan_guard_until = -1
        guard_demote_once = False
        cold_extra_applied = False
        p_adjusted_this_access = True
    else:
        # Brand-new: do NOT change p here; optionally open a short guard window on long cold streaks
        if cold_streak >= max(1, C // 2):
            scan_guard_until = max(scan_guard_until, cache_snapshot.access_count + _guard_window(C))

    # ARC REPLACE with guard-adjusted effective p
    t1_sz = len(arc_T1)
    guard_active = (scan_guard_until != -1 and cache_snapshot.access_count < scan_guard_until)
    # Gentle effective_p drop under guard with softer, dynamic window
    threshold = max(1, C // 2)
    unit = max(1, C // 16)
    extra = 0
    if guard_active:
        extra = min(unit, 1 + max(0, cold_streak - threshold) // unit)
    p_eff = max(0, arc_p - extra)
    # One-shot demotion bias when scans likely and no freq history (B2 empty)
    if guard_active and len(arc_B2) == 0 and len(arc_T2) >= len(arc_T1) and not guard_demote_once:
        p_eff = 0
        guard_demote_once = True

    candidate = None
    last_replaced_from = None
    if t1_sz >= 1 and (t1_sz > p_eff or (in_B2 and t1_sz == p_eff)):
        # Evict LRU from T1
        candidate = next(iter(arc_T1)) if arc_T1 else None
        if candidate is not None:
            last_replaced_from = 'T1'
    else:
        # Evict LRU from T2
        candidate = next(iter(arc_T2)) if arc_T2 else None
        if candidate is not None:
            last_replaced_from = 'T2'

    # Deterministic, depth-limited fallbacks with ghost hints
    if candidate is None:
        # Try to avoid removing B2-hinted keys from T1
        for k in list(arc_T1.keys()):
            if k not in arc_B2:
                candidate = k
                last_replaced_from = 'T1'
                break
    if candidate is None:
        # Prefer T2 keys that appear in B1 (recency-only hint)
        for k in list(arc_T2.keys()):
            if k in arc_B1:
                candidate = k
                last_replaced_from = 'T2'
                break
    if candidate is None:
        # Depth-limited peek
        budget = min(8, max(1, C // 16))
        cnt = 0
        for k in arc_T1.keys():
            if k not in arc_B2:
                candidate = k
                last_replaced_from = 'T1'
                break
            cnt += 1
            if cnt >= budget:
                break
        if candidate is None:
            cnt = 0
            for k in arc_T2.keys():
                if k in arc_B1:
                    candidate = k
                    last_replaced_from = 'T2'
                    break
                cnt += 1
                if cnt >= budget:
                    break
    if candidate is None:
        # Timestamp tie-breaker restricted to T1 keys first
        min_ts = float('inf')
        min_k = None
        for k in arc_T1.keys():
            ts = m_key_timestamp.get(k, float('inf'))
            if ts < min_ts:
                min_ts = ts
                min_k = k
        if min_k is not None:
            candidate = min_k
            last_replaced_from = 'T1'
    if candidate is None and m_key_timestamp:
        # Fallback timestamp across all cached keys
        min_ts = float('inf')
        min_k = None
        for k in cache_snapshot.cache.keys():
            ts = m_key_timestamp.get(k, float('inf'))
            if ts < min_ts:
                min_ts = ts
                min_k = k
        candidate = min_k
        # Set source if we can infer it
        if candidate in arc_T1:
            last_replaced_from = 'T1'
        elif candidate in arc_T2:
            last_replaced_from = 'T2'
    if candidate is None and cache_snapshot.cache:
        # Last resort: arbitrary
        candidate = next(iter(cache_snapshot.cache.keys()))
        if candidate in arc_T1:
            last_replaced_from = 'T1'
        elif candidate in arc_T2:
            last_replaced_from = 'T2'
    return candidate


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata immediately after a cache hit.
    '''
    global m_key_timestamp, cold_streak, scan_guard_until, guard_demote_once, p_adjusted_this_access
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    # ARC: on hit, move to T2 MRU
    key = obj.key
    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # If already in T2, refresh; if not present due to drift, place in T2
        _move_to_mru(arc_T2, key)

    # Resident keys must not exist in ghosts
    arc_B1.pop(key, None)
    arc_B2.pop(key, None)

    # Any hit breaks a cold streak and cancels scan guard and one-shot bias
    cold_streak = 0
    scan_guard_until = -1
    guard_demote_once = False

    # Update timestamp for tie-breaking/fallback
    m_key_timestamp[key] = cache_snapshot.access_count
    # Reset per-access p-adjustment flag
    p_adjusted_this_access = False


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata immediately after inserting a new object into the cache.
    '''
    global m_key_timestamp, cold_streak, scan_guard_until, guard_demote_once, last_ghost_hit_access, cold_extra_applied, arc_p, p_adjusted_this_access
    _ensure_capacity(cache_snapshot)
    _decay_p_if_idle(cache_snapshot)

    key = obj.key
    C = arc_capacity if arc_capacity else 1
    guard_active = (scan_guard_until != -1 and cache_snapshot.access_count < scan_guard_until)

    # ARC admission policy: ghost hits go to T2 (p already adjusted in evict)
    if key in arc_B1 or key in arc_B2:
        # Canonical ghost-driven p updates if evict didn't do it
        if not p_adjusted_this_access:
            if key in arc_B1:
                denom = max(1, len(arc_B1))
                step_up = (len(arc_B2) + denom - 1) // denom
                arc_p = min(C, arc_p + min(step_up, max(1, C // 8)))
            else:
                denom = max(1, len(arc_B2))
                step_down = (len(arc_B1) + denom - 1) // denom
                dec_cap = max(1, (C // 4) if cold_streak >= max(1, C // 2) else (C // 8))
                arc_p = max(0, arc_p - min(step_down, dec_cap))
            last_ghost_hit_access = cache_snapshot.access_count
            guard_demote_once = False
            cold_extra_applied = False
        cold_streak = 0
        scan_guard_until = -1
        # keep ghosts disjoint
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1; during guard, insert at LRU to reduce pollution
        cold_streak += 1
        if guard_active:
            _insert_at_lru(arc_T1, key)
        else:
            _move_to_mru(arc_T1, key)
        # If long cold streak and no active guard, open a short guard window
        if cold_streak >= max(1, C // 2) and not guard_active:
            scan_guard_until = max(scan_guard_until, cache_snapshot.access_count + _guard_window(C))
        # Ensure ghosts are disjoint from residents
        arc_B1.pop(key, None)
        arc_B2.pop(key, None)

    _trim_ghosts()
    m_key_timestamp[key] = cache_snapshot.access_count
    # Reset per-access p-adjustment flag
    p_adjusted_this_access = False


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata immediately after evicting the victim.
    '''
    global m_key_timestamp, last_replaced_from
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key

    # Place evicted resident into corresponding ghost list using remembered source
    if last_replaced_from == 'T1':
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
        arc_B2.pop(k, None)
    elif last_replaced_from == 'T2':
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
        arc_B1.pop(k, None)
    else:
        # Fallback by checking membership (robustness)
        if k in arc_T1:
            arc_T1.pop(k, None)
            _move_to_mru(arc_B1, k)
            arc_B2.pop(k, None)
        elif k in arc_T2:
            arc_T2.pop(k, None)
            _move_to_mru(arc_B2, k)
            arc_B1.pop(k, None)
        else:
            _move_to_mru(arc_B1, k)
            arc_B2.pop(k, None)

    # Clean up
    last_replaced_from = None
    m_key_timestamp.pop(k, None)
    _trim_ghosts()

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