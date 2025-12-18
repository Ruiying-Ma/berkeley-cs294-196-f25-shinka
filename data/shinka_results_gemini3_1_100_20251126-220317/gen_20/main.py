# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

import random
import math

from collections import OrderedDict, defaultdict

# W-TinyLFU State
class WTinyLFUState:
    def __init__(self):
        self.window = OrderedDict()      # Window Cache (LRU)
        self.probation = OrderedDict()   # Main Cache - Probation (LRU)
        self.protected = OrderedDict()   # Main Cache - Protected (LRU)
        self.freq = defaultdict(int)     # Frequency counters
        self.doorkeeper = set()          # Doorkeeper bloom-filter (set)
        self.access_count = 0
        self.pending_promote = None      # Key to promote from Window to Probation
        self.max_time = 0

algo_state = WTinyLFUState()

def _check_reset(current_time):
    if current_time < algo_state.max_time:
        algo_state.window.clear()
        algo_state.probation.clear()
        algo_state.protected.clear()
        algo_state.freq.clear()
        algo_state.doorkeeper.clear()
        algo_state.access_count = 0
        algo_state.pending_promote = None
        algo_state.max_time = 0
    algo_state.max_time = current_time

def _get_freq(key):
    return algo_state.freq[key]

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction
    '''
    state = algo_state
    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # Identify candidates
    victim_w = next(iter(state.window)) if (len(state.window) >= w_cap and state.window) else None

    victim_m = None
    if state.probation:
        victim_m = next(iter(state.probation))
    elif state.protected:
        victim_m = next(iter(state.protected))

    state.pending_promote = None

    # 1. Window not full: Evict from Main to make room for Window
    if victim_w is None:
        if victim_m: return victim_m
        if state.window: return next(iter(state.window)) # Fallback
        if cache_snapshot.cache: return next(iter(cache_snapshot.cache))
        return None

    # 2. Window full: Duel between Window LRU and Main LRU
    if victim_m is None:
        return victim_w

    fw = _get_freq(victim_w)
    fm = _get_freq(victim_m)

    if fw > fm:
        state.pending_promote = victim_w
        return victim_m
    else:
        return victim_w

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    state = algo_state
    key = obj.key
    state.access_count += 1

    # Frequency Update
    if key not in state.doorkeeper:
        state.doorkeeper.add(key)
    else:
        state.freq[key] += 1

    # Aging
    if state.access_count % (cache_snapshot.capacity * 10) == 0:
        state.doorkeeper.clear()
        for k in list(state.freq):
            state.freq[k] //= 2
            if state.freq[k] == 0: del state.freq[k]

    # Location Update
    if key in state.window:
        state.window.move_to_end(key)
    elif key in state.probation:
        del state.probation[key]
        state.protected[key] = True
    elif key in state.protected:
        state.protected.move_to_end(key)

    # Enforce Protected Size (80% of Main)
    w_cap = max(1, int(cache_snapshot.capacity * 0.01))
    main_cap = cache_snapshot.capacity - w_cap
    protected_cap = int(main_cap * 0.8)

    while len(state.protected) > protected_cap:
        k, _ = state.protected.popitem(last=False)
        state.probation[k] = True

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    state = algo_state
    key = obj.key

    state.window[key] = True

    state.access_count += 1
    if key not in state.doorkeeper:
        state.doorkeeper.add(key)
    else:
        state.freq[key] += 1

def update_after_evict(cache_snapshot, obj, evicted_obj):
    state = algo_state
    key = evicted_obj.key

    if key in state.window:
        del state.window[key]
    elif key in state.probation:
        del state.probation[key]
    elif key in state.protected:
        del state.protected[key]

    if state.pending_promote:
        p_key = state.pending_promote
        if p_key in state.window:
            del state.window[p_key]
            state.probation[p_key] = True
        state.pending_promote = None

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