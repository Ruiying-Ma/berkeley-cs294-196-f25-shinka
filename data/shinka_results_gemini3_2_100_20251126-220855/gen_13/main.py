# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict

# LIRS Global State
# m_s: Stack S. Stores LIR and HIR blocks. Ordered by recency.
#      Keys: object keys. Values: None (dummy). MRU at end.
# m_q: Queue Q. Stores Resident HIR blocks. Keys: object keys. MRU at end.
# m_lir: Set of keys that are currently LIR status.
m_s = OrderedDict()
m_q = OrderedDict()
m_lir = set()
m_last_access_count = 0

# Constants
HIR_RATIO = 0.01

def check_reset(cache_snapshot):
    global m_last_access_count, m_s, m_q, m_lir
    if cache_snapshot.access_count < m_last_access_count:
        m_s.clear()
        m_q.clear()
        m_lir.clear()
    m_last_access_count = cache_snapshot.access_count

def prune_stack():
    '''Ensure the bottom of Stack S is a LIR block.'''
    global m_s, m_lir
    while m_s:
        k = next(iter(m_s))
        if k not in m_lir:
            m_s.popitem(last=False)
        else:
            break

def evict(cache_snapshot, obj):
    '''
    LIRS Eviction:
    - Prefer evicting Resident HIR (front of Q).
    - If needed, evict LIR (bottom of S).
    '''
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir

    # Ensure S is pruned
    prune_stack()

    # Victim from Q (Resident HIR)
    if m_q:
        return next(iter(m_q))

    # Victim from LIR (if Q empty)
    if m_s:
        return next(iter(m_s))

    # Fallback (should not be reached)
    return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = obj.key
    capacity = cache_snapshot.capacity
    target_lir = max(1, int(capacity * (1.0 - HIR_RATIO)))

    if key in m_lir:
        # LIR Hit
        if key in m_s:
            m_s.move_to_end(key)
        else:
            # Restoration if desynced
            m_s[key] = None
        prune_stack()

    elif key in m_q:
        # Resident HIR Hit
        if key in m_s:
            # HIR in Stack -> Promote to LIR
            m_lir.add(key)
            del m_q[key]
            m_s.move_to_end(key)

            # Demote if needed
            if len(m_lir) > target_lir:
                # Bottom of S is the LIR to demote (due to prune)
                demoted = next(iter(m_s))
                m_lir.remove(demoted)
                m_s.popitem(last=False)
                m_q[demoted] = None
                # Prune S again because new bottom might be HIR
                prune_stack()
        else:
            # Stay HIR
            m_q.move_to_end(key)
            m_s[key] = None

    else:
        # Access to item in cache but not in our structures (shouldn't happen)
        # Treat as new insert
        m_q[key] = None
        m_s[key] = None

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = obj.key
    capacity = cache_snapshot.capacity
    target_lir = max(1, int(capacity * (1.0 - HIR_RATIO)))

    if key in m_s:
        # Non-Resident HIR -> Promote
        m_lir.add(key)
        m_s.move_to_end(key)

        if len(m_lir) > target_lir:
            demoted = next(iter(m_s))
            m_lir.remove(demoted)
            m_s.popitem(last=False)
            m_q[demoted] = None
            prune_stack()
    else:
        # New -> HIR
        m_q[key] = None
        m_s[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    check_reset(cache_snapshot)
    global m_s, m_q, m_lir
    key = evicted_obj.key

    if key in m_q:
        del m_q[key]
    if key in m_lir:
        m_lir.remove(key)
        if key in m_s:
            del m_s[key]
            prune_stack()

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