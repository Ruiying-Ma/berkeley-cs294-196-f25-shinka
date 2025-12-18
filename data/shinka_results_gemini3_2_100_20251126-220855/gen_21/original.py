# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict

# ARC Global State
arc_t1 = OrderedDict() # T1: Recent items
arc_t2 = OrderedDict() # T2: Frequent items
arc_b1 = OrderedDict() # B1: Ghost Recent
arc_b2 = OrderedDict() # B2: Ghost Frequent
arc_p = 0.0            # Adaptation parameter
arc_c = 0              # Cache capacity
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global arc_t1, arc_t2, arc_b1, arc_b2, arc_p, arc_c, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        arc_t1.clear()
        arc_t2.clear()
        arc_b1.clear()
        arc_b2.clear()
        arc_p = 0.0
        arc_c = cache_snapshot.capacity
    m_last_access_count = cache_snapshot.access_count
    if arc_c == 0: arc_c = cache_snapshot.capacity

def evict(cache_snapshot, obj):
    '''
    ARC Eviction Policy.
    Uses T1/T2 lists and B1/B2 ghost lists to adaptively manage cache content.
    '''
    _check_reset(cache_snapshot)
    global arc_p

    key = obj.key
    # Adaptation logic (ARC)
    if key in arc_b1:
        delta = 1
        if len(arc_b1) >= len(arc_b2):
            delta = 1
        else:
            delta = len(arc_b2) / len(arc_b1)
        arc_p = min(float(arc_c), arc_p + delta)
    elif key in arc_b2:
        delta = 1
        if len(arc_b2) >= len(arc_b1):
            delta = 1
        else:
            delta = len(arc_b1) / len(arc_b2)
        arc_p = max(0.0, arc_p - delta)

    # Determine victim based on ARC logic
    replace_t1 = False
    if len(arc_t1) > 0:
        if len(arc_t1) > arc_p:
            replace_t1 = True
        elif (key in arc_b1) and (len(arc_t1) == int(arc_p)):
             replace_t1 = True

    if replace_t1 and arc_t1:
        return next(iter(arc_t1)) # LRU of T1
    else:
        if arc_t2:
            return next(iter(arc_t2)) # LRU of T2
        return next(iter(arc_t1)) # Fallback

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Move to MRU of T2.
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    if key in arc_t1:
        del arc_t1[key]
        arc_t2[key] = 1 # Insert MRU T2
    elif key in arc_t2:
        arc_t2.move_to_end(key) # Update MRU T2

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert: Move ghosts to T2, or new items to T1.
    '''
    _check_reset(cache_snapshot)
    key = obj.key

    if key in arc_b1:
        del arc_b1[key]
        arc_t2[key] = 1 # From Ghost T1 -> T2
    elif key in arc_b2:
        del arc_b2[key]
        arc_t2[key] = 1 # From Ghost T2 -> T2
    else:
        arc_t1[key] = 1 # New item -> T1

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Move to Ghost lists and manage Ghost capacity.
    '''
    key = evicted_obj.key

    if key in arc_t1:
        del arc_t1[key]
        arc_b1[key] = 1
    elif key in arc_t2:
        del arc_t2[key]
        arc_b2[key] = 1

    # Manage Ghost Capacity
    # Limit B1 and B2 size to ensure bounded memory usage
    target_ghost = arc_c

    while len(arc_b1) > target_ghost:
        arc_b1.popitem(last=False)
    while len(arc_b2) > target_ghost * 2:
        arc_b2.popitem(last=False)

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