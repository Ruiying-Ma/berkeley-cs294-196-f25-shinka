# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import deque

# ARC State
algo_state = {
    't1': {},    # key -> None (OrderedDict behavior for LRU)
    't2': {},    # key -> None
    'b1': {},    # key -> None
    'b2': {},    # key -> None
    'p': 0.0,    # Target size of T1
    'max_time': 0
}

def _check_reset(current_time):
    if current_time < algo_state['max_time']:
        algo_state['t1'].clear()
        algo_state['t2'].clear()
        algo_state['b1'].clear()
        algo_state['b2'].clear()
        algo_state['p'] = 0.0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    ARC Eviction Logic.
    Decides whether to evict from T1 or T2 based on target size p and ghost hits.
    '''
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    p = algo_state['p']
    key = obj.key
    capacity = cache_snapshot.capacity

    # Calculate effective p for this decision (simulate adaptation)
    # We do not modify global p here, that happens in update_after_insert
    curr_p = p
    if key in b1:
        delta = 1
        if len(b2) > len(b1):
            delta = len(b2) / len(b1)
        curr_p = min(capacity, p + delta)
    elif key in b2:
        delta = 1
        if len(b1) > len(b2):
            delta = len(b1) / len(b2)
        curr_p = max(0, p - delta)

    # Decide victim
    # Logic: if (|T1| > p) or (x in B2 and |T1| == p) -> Evict T1
    evict_t1 = False
    if len(t1) > 0 and (len(t1) > curr_p or (key in b2 and len(t1) == int(curr_p))):
        evict_t1 = True
    elif len(t2) == 0:
        evict_t1 = True

    if evict_t1:
        return next(iter(t1))
    else:
        return next(iter(t2))

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
        t2[key] = None
    elif key in t2:
        del t2[key]
        t2[key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    Insert: Handle ghost hits, update p, insert to T1 or T2.
    '''
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    p = algo_state['p']
    capacity = cache_snapshot.capacity

    # Adapt p and move from ghost if needed
    if key in b1:
        delta = 1
        if len(b2) > len(b1):
            delta = len(b2) / len(b1)
        algo_state['p'] = min(capacity, p + delta)

        del b1[key]
        t2[key] = None # Move to T2 MRU

    elif key in b2:
        delta = 1
        if len(b1) > len(b2):
            delta = len(b1) / len(b2)
        algo_state['p'] = max(0, p - delta)

        del b2[key]
        t2[key] = None # Move to T2 MRU

    else:
        # New item -> T1 MRU
        t1[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict: Move victim to ghost list and enforce ghost capacity.
    '''
    v_key = evicted_obj.key
    t1 = algo_state['t1']
    t2 = algo_state['t2']
    b1 = algo_state['b1']
    b2 = algo_state['b2']
    capacity = cache_snapshot.capacity

    # Identify where it was and move to ghost
    if v_key in t1:
        del t1[v_key]
        b1[v_key] = None
    elif v_key in t2:
        del t2[v_key]
        b2[v_key] = None

    # Enforce ghost capacity
    while len(b1) + len(b2) > capacity:
        if len(b1) > len(t2):
            if b1:
                del b1[next(iter(b1))]
            elif b2:
                del b2[next(iter(b2))]
        else:
            if b2:
                del b2[next(iter(b2))]
            elif b1:
                del b1[next(iter(b1))]

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