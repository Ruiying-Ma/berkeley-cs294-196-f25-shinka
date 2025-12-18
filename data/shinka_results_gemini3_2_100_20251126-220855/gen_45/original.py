# EVOLVE-BLOCK-START
from collections import OrderedDict

# Global ARC State
# m_t1: Recent items (in cache)
# m_t2: Frequent items (in cache)
# m_b1: Ghost Recent (evicted history)
# m_b2: Ghost Frequent (evicted history)
# m_p: Target size for T1
m_t1 = OrderedDict()
m_t2 = OrderedDict()
m_b1 = OrderedDict()
m_b2 = OrderedDict()
m_p = 0.0

def evict(cache_snapshot, obj):
    '''
    ARC Eviction Policy.
    - Adapts m_p based on hits in ghost lists (B1/B2).
    - Selects victim from T1 or T2 based on target size m_p.
    '''
    global m_p
    capacity = cache_snapshot.capacity
    key = obj.key

    # 1. Adapt m_p (Target T1 size)
    # If hit in B1 (Recency Ghost), increase p (favor Recency)
    # If hit in B2 (Frequency Ghost), decrease p (favor Frequency)
    if key in m_b1:
        delta = 1.0
        if len(m_b1) < len(m_b2):
            delta = float(len(m_b2)) / len(m_b1)
        m_p = min(float(capacity), m_p + delta)
    elif key in m_b2:
        delta = 1.0
        if len(m_b2) < len(m_b1):
            delta = float(len(m_b1)) / len(m_b2)
        m_p = max(0.0, m_p - delta)

    # 2. Select Victim
    # We evict from T1 if it exceeds the target size p,
    # or under specific boundary conditions implied by ARC.
    replace_p = m_p
    len_t1 = len(m_t1)

    evict_t1 = False
    if len_t1 > 0:
        if len_t1 > replace_p:
            evict_t1 = True
        elif (key in m_b2) and (len_t1 == int(replace_p)):
             # If we hit B2, we want to shrink T1, so if we are at the limit, evict T1.
             evict_t1 = True

    # If T2 is empty, we must evict from T1 regardless of p
    if evict_t1 or not m_t2:
        return next(iter(m_t1))
    else:
        return next(iter(m_t2))

def update_after_hit(cache_snapshot, obj):
    '''
    On hit, move to MRU of T2 (Frequent list).
    '''
    key = obj.key
    if key in m_t1:
        del m_t1[key]
        m_t2[key] = None
    elif key in m_t2:
        m_t2.move_to_end(key)
    else:
        # Fallback for sync issues, though shouldn't occur
        m_t2[key] = None

def update_after_insert(cache_snapshot, obj):
    '''
    On insert, place in T1 or promote ghost to T2.
    '''
    key = obj.key
    if key in m_b1:
        del m_b1[key]
        m_t2[key] = None
    elif key in m_b2:
        del m_b2[key]
        m_t2[key] = None
    else:
        m_t1[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Move evicted item to corresponding ghost list (B1 or B2).
    Maintain ghost list sizes.
    '''
    key = evicted_obj.key
    capacity = cache_snapshot.capacity

    if key in m_t1:
        del m_t1[key]
        m_b1[key] = None
    elif key in m_t2:
        del m_t2[key]
        m_b2[key] = None

    # Lazy cleanup of ghost lists to keep memory bounded
    if len(m_b1) > capacity:
        m_b1.popitem(last=False)
    if len(m_b2) > capacity * 2:
        m_b2.popitem(last=False)

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