# EVOLVE-BLOCK-START
"""ARC+LFU(aged) eviction with proportional ghost control and strict invariants.

Architecture:
- Central policy state encapsulated in module-level dicts, managed via helpers.
- ARC resident sets: T1 (recent), T2 (frequent).
- Ghost sets: B1 (evicted from T1), B2 (evicted from T2), sized to 2×capacity with
  p-aware proportional trimming for sharper steering.
- p is target size for T1 (0..capacity). On B1/B2 hits, adjust p asymmetrically with
  bounded steps to avoid overshoot.
- T2 victim selection uses effective frequency with time-decay, LRU tiebreak.

Invariants enforced:
- |T1| + |T2| == resident cache size (resync on drift).
- T-sets and B-sets are disjoint (remove from ghosts on admission/promotion).
- Ghosts bounded and trimmed after updates.
"""

from collections import OrderedDict

# Policy state
PS = {
    "cap": None,             # capacity (items)
    "p": 0,                  # ARC target size for T1
    "T1": OrderedDict(),     # resident recent
    "T2": OrderedDict(),     # resident frequent
    "B1": OrderedDict(),     # ghost of T1 (key -> True)
    "B2": OrderedDict(),     # ghost of T2 (key -> True)
    "ts": dict(),            # key -> last access timestamp (for recency/age)
    "f2": dict(),            # T2 frequency counters
}

# ------------- Helpers and invariants ------------- #

def _ensure_cap(cache_snapshot):
    if PS["cap"] is None:
        PS["cap"] = max(int(cache_snapshot.capacity), 1)
        # Start p in the middle for faster stabilization
        PS["p"] = PS["cap"] // 2

def _move_mru(od: OrderedDict, key: str):
    if key in od:
        od.pop(key, None)
    od[key] = True

def _pop_lru(od: OrderedDict):
    if not od:
        return None
    k, _ = od.popitem(last=False)
    return k

def _resync(cache_snapshot):
    """Align resident metadata with actual cache contents and enforce disjointness."""
    cache_keys = set(cache_snapshot.cache.keys())
    # Remove non-resident from T1/T2
    for k in list(PS["T1"].keys()):
        if k not in cache_keys:
            PS["T1"].pop(k, None)
            PS["ts"].pop(k, None)
    for k in list(PS["T2"].keys()):
        if k not in cache_keys:
            PS["T2"].pop(k, None)
            PS["ts"].pop(k, None)
            PS["f2"].pop(k, None)
    # Add any resident keys missing in metadata into T1
    for k in cache_keys:
        if k not in PS["T1"] and k not in PS["T2"]:
            _move_mru(PS["T1"], k)
    # Ensure disjointness with ghosts: remove any resident from B1/B2
    for k in list(PS["B1"].keys()):
        if k in PS["T1"] or k in PS["T2"]:
            PS["B1"].pop(k, None)
    for k in list(PS["B2"].keys()):
        if k in PS["T1"] or k in PS["T2"]:
            PS["B2"].pop(k, None)
    _trim_ghosts()

def _ghost_targets():
    """Compute proportional ghost targets based on current p: |B1| target = 2p, |B2| = 2C-2p."""
    C = PS["cap"] if PS["cap"] is not None else 1
    p = max(0, min(C, PS["p"]))
    total_ghost_cap = 2 * C
    target_B1 = min(total_ghost_cap, 2 * p)
    target_B2 = total_ghost_cap - target_B1
    return target_B1, target_B2, total_ghost_cap

def _trim_ghosts():
    """Keep |B1|+|B2| ≤ 2C with proportional trimming around p."""
    C = PS["cap"] if PS["cap"] is not None else 1
    target_B1, target_B2, total_cap = _ghost_targets()
    # Trim until within total cap
    while len(PS["B1"]) + len(PS["B2"]) > total_cap:
        # Prefer trimming the list exceeding its proportional target
        if len(PS["B1"]) > target_B1:
            _pop_lru(PS["B1"])
        elif len(PS["B2"]) > target_B2:
            _pop_lru(PS["B2"])
        else:
            # Otherwise trim the larger ghost
            if len(PS["B1"]) >= len(PS["B2"]):
                _pop_lru(PS["B1"])
            else:
                _pop_lru(PS["B2"])

def _bounded_inc_dec(x, delta, lo, hi):
    return max(lo, min(hi, x + delta))

def _adjust_p_on_ghost_hit(in_B1: bool, in_B2: bool):
    """Adjust p asymmetrically, bounded to avoid runaway."""
    C = PS["cap"] if PS["cap"] is not None else 1
    p = PS["p"]
    if in_B1:
        # Increase p, favor recency; step bounded by both ratio and C/8 and remaining headroom
        ratio_step = max(1, len(PS["B2"]) // max(1, len(PS["B1"])))
        step_cap = max(1, C // 8)
        inc = min(step_cap, ratio_step, C - p)
        PS["p"] = _bounded_inc_dec(p, inc, 0, C)
    elif in_B2:
        # Decrease p, favor frequency
        ratio_step = max(1, len(PS["B1"]) // max(1, len(PS["B2"])))
        step_cap = max(1, C // 8)
        dec = min(step_cap, ratio_step, p)
        PS["p"] = _bounded_inc_dec(p, -dec, 0, C)

def _eff_freq(cache_snapshot, k: str):
    """Aged frequency used for T2 victim selection."""
    now = cache_snapshot.access_count
    freq = PS["f2"].get(k, 1)
    last = PS["ts"].get(k, now)
    staleness = max(0, now - last)
    # Window scales with capacity; larger windows reduce over-aging
    window = max(1, (PS["cap"] if PS["cap"] is not None else 1) // 2)
    aged = max(1, freq - (staleness // window))
    return aged, last  # return tuple for tiebreaking

def _choose_t2_victim(cache_snapshot):
    """Select T2 victim with lowest aged frequency; break ties by oldest last access."""
    if not PS["T2"]:
        return None
    best_k = None
    best_tuple = None
    for k in PS["T2"].keys():
        tup = _eff_freq(cache_snapshot, k)
        # Compare on (eff_freq, last_ts) — smaller is worse
        if best_tuple is None or tup < best_tuple:
            best_tuple = tup
            best_k = k
    return best_k

def _replace_should_evict_T1(obj_key: str):
    """ARC REPLACE decision; returns True to evict from T1, else T2."""
    t1_sz = len(PS["T1"])
    # Strict ARC rule
    if t1_sz and (t1_sz > PS["p"] or (obj_key in PS["B2"] and t1_sz == PS["p"])):
        return True
    return False

# ------------- Required API ------------- #

def evict(cache_snapshot, obj):
    '''
    Decide eviction victim key using strict ARC REPLACE with LFU-aged T2.
    '''
    _ensure_cap(cache_snapshot)
    _resync(cache_snapshot)

    # Primary ARC choice
    evict_T1 = _replace_should_evict_T1(obj.key)

    victim = None
    if evict_T1:
        # Evict LRU from T1
        victim = next(iter(PS["T1"])) if PS["T1"] else None
        if victim is None and PS["T2"]:
            # Fallback to T2 if T1 empty
            victim = _choose_t2_victim(cache_snapshot)
    else:
        # Evict from T2 with LFU-aged selection
        victim = _choose_t2_victim(cache_snapshot)
        if victim is None and PS["T1"]:
            victim = next(iter(PS["T1"]))
    # Last resort: if both empty (drift), resync one more time and pick any
    if victim is None:
        _resync(cache_snapshot)
        if PS["T1"]:
            victim = next(iter(PS["T1"]))
        elif PS["T2"]:
            victim = _choose_t2_victim(cache_snapshot)
        elif cache_snapshot.cache:
            victim = next(iter(cache_snapshot.cache.keys()))
    return victim


def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata on cache hit: promote T1→T2, refresh recency, bump T2 freq.
    '''
    _ensure_cap(cache_snapshot)
    _resync(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count

    # Refresh timestamp
    PS["ts"][key] = now

    if key in PS["T1"]:
        # Promote to T2 on first hit (canonical ARC)
        PS["T1"].pop(key, None)
        _move_mru(PS["T2"], key)
        PS["f2"][key] = PS["f2"].get(key, 1) + 1
    elif key in PS["T2"]:
        _move_mru(PS["T2"], key)
        PS["f2"][key] = PS["f2"].get(key, 1) + 1
    else:
        # Drift: if a resident but not tracked, treat as T2 to protect
        if key in cache_snapshot.cache:
            _move_mru(PS["T2"], key)
            PS["f2"][key] = PS["f2"].get(key, 1) + 1

    # Remove from ghosts if present (disjointness)
    PS["B1"].pop(key, None)
    PS["B2"].pop(key, None)
    _trim_ghosts()


def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata on new insertion (after optional eviction).
    Handles B1/B2 ghost hits with bounded p-updates and disjointness.
    '''
    _ensure_cap(cache_snapshot)
    _resync(cache_snapshot)
    key = obj.key
    now = cache_snapshot.access_count

    in_B1 = key in PS["B1"]
    in_B2 = key in PS["B2"]

    if in_B1 or in_B2:
        # Adjust p according to ghost hit type
        _adjust_p_on_ghost_hit(in_B1, in_B2)
        # On ghost hit, admit to T2
        PS["B1"].pop(key, None)
        PS["B2"].pop(key, None)
        _move_mru(PS["T2"], key)
        PS["f2"][key] = PS["f2"].get(key, 1) + 1
    else:
        # Cold admission goes to T1
        _move_mru(PS["T1"], key)
        # Remove any lingering ghost entry
        PS["B1"].pop(key, None)
        PS["B2"].pop(key, None)

    PS["ts"][key] = now
    _trim_ghosts()


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    After evicting a resident, move it to the appropriate ghost list and clean state.
    Maintain ghost disjointness and proportional bounds.
    '''
    _ensure_cap(cache_snapshot)
    if evicted_obj is None:
        return
    k = evicted_obj.key

    # Identify residency and move to proper ghost
    moved = False
    if k in PS["T1"]:
        PS["T1"].pop(k, None)
        # keep ghosts disjoint
        PS["B2"].pop(k, None)
        _move_mru(PS["B1"], k)
        moved = True
    elif k in PS["T2"]:
        PS["T2"].pop(k, None)
        PS["f2"].pop(k, None)
        PS["B1"].pop(k, None)
        _move_mru(PS["B2"], k)
        moved = True
    else:
        # Unknown membership: prefer B2 if it already exists, else B1
        if k in PS["B2"]:
            _move_mru(PS["B2"], k)
        else:
            PS["B2"].pop(k, None)
            _move_mru(PS["B1"], k)

    # Remove transient metadata
    PS["ts"].pop(k, None)

    # Enforce ghost bounds
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