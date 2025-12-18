# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

import random
import math

# ARC State
algo_state = {
    't1': {},            # LRU list (dict) for T1 (Recent)
    't2': {},            # LRU list (dict) for T2 (Frequent)
    'b1': {},            # LRU list (dict) for B1 (Ghost Recent)
    'b2': {},            # LRU list (dict) for B2 (Ghost Frequent)
    'p': 0,              # Target size for T1
    'max_time': 0,       # Track time to detect trace resets
}

def _check_reset(current_time):
    # If time goes backwards, we are likely processing a new trace
    if current_time < algo_state['max_time']:
        algo_state['t1'].clear()
        algo_state['t2'].clear()
        algo_state['b1'].clear()
        algo_state['b2'].clear()
        algo_state['p'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    ARC Eviction Policy
    '''
    key = obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    p = algo_state['p']
    capacity = cache_snapshot.capacity

    # Adaptation simulation for decision
    p_eff = p
    if key in b1:
        d = 1
        if len(b1) >= len(b2):
            d = 1
        else:
            d = len(b2) / len(b1)
        p_eff = min(capacity, p + d)
    elif key in b2:
        d = 1
        if len(b2) >= len(b1):
            d = 1
        else:
            d = len(b1) / len(b2)
        p_eff = max(0, p - d)

    # REPLACE logic
    replace_t1 = False
    if len(t1) > 0:
        if len(t1) > p_eff:
            replace_t1 = True
        elif (key in b2) and (len(t1) == int(p_eff)):
            replace_t1 = True

    candid = None
    if replace_t1:
        candid = next(iter(t1))
    elif len(t2) > 0:
        candid = next(iter(t2))
    elif len(t1) > 0:
        candid = next(iter(t1))

    if candid is None and cache_snapshot.cache:
         candid = next(iter(cache_snapshot.cache))

    return candid

def update_after_hit(cache_snapshot, obj):
    '''
    Hit: Move to T2 MRU.
    '''
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']

    if key in t1:
        del t1[key]
        t2[key] = True
    elif key in t2:
        del t2[key]
        t2[key] = True
    else:
        # Failsafe
        t2[key] = True

def update_after_insert(cache_snapshot, obj):
    '''
    Insert: Update p and lists.
    '''
    _check_reset(cache_snapshot.access_count)

    key = obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    p = algo_state['p']
    capacity = cache_snapshot.capacity

    # Update p (Adaptation)
    if key in b1:
        d = 1
        if len(b1) >= len(b2):
            d = 1
        else:
            d = len(b2) / len(b1)
        p = min(capacity, p + d)
        del b1[key]
        t2[key] = True
    elif key in b2:
        d = 1
        if len(b2) >= len(b1):
            d = 1
        else:
            d = len(b1) / len(b2)
        p = max(0, p - d)
        del b2[key]
        t2[key] = True
    else:
        t1[key] = True

    algo_state['p'] = p

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict: Update T1/T2 and B1/B2.
    '''
    ekey = evicted_obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    capacity = cache_snapshot.capacity

    if ekey in t1:
        del t1[ekey]
        b1[ekey] = True
    elif ekey in t2:
        del t2[ekey]
        b2[ekey] = True

    # Trim ghosts
    if len(b1) > capacity:
        del b1[next(iter(b1))]

    if len(b2) > capacity * 2:
        del b2[next(iter(b2))]

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