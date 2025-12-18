# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# LIRS (Low Inter-reference Recency Set) Global State
m_lirs_S = {}        # OrderedDict: key -> None (Stack S: Recency of LIR + history HIR)
m_lirs_Q = {}        # OrderedDict: key -> None (Queue Q: Resident HIRs, LRU to MRU)
m_lirs_type = {}     # dict: key -> 'LIR' or 'HIR'
m_lirs_count = 0     # int: number of LIR blocks
m_lirs_limit = 0     # int: target LIR capacity
m_last_access_count = 0

def check_reset(cache_snapshot):
    global m_last_access_count, m_lirs_S, m_lirs_Q, m_lirs_type, m_lirs_count
    current_count = cache_snapshot.access_count
    if current_count < m_last_access_count:
        m_lirs_S.clear()
        m_lirs_Q.clear()
        m_lirs_type.clear()
        m_lirs_count = 0
    m_last_access_count = current_count

def prune_lirs_S():
    global m_lirs_S, m_lirs_type
    while m_lirs_S:
        k = next(iter(m_lirs_S))
        if m_lirs_type.get(k) == 'LIR':
            break
        del m_lirs_S[k]
        if k not in m_lirs_Q:
             if k in m_lirs_type: del m_lirs_type[k]

def demote_bottom_lir():
    global m_lirs_S, m_lirs_Q, m_lirs_type, m_lirs_count
    if not m_lirs_S: return
    k = next(iter(m_lirs_S))
    if m_lirs_type.get(k) == 'LIR':
        m_lirs_type[k] = 'HIR'
        m_lirs_count -= 1
        if k in m_lirs_Q: del m_lirs_Q[k]
        m_lirs_Q[k] = None
        prune_lirs_S()

def access_lirs(cache_snapshot, key):
    global m_lirs_S, m_lirs_Q, m_lirs_type, m_lirs_count, m_lirs_limit

    # Update limit dynamically
    m_lirs_limit = max(1, int(cache_snapshot.capacity * 0.99))

    is_lir = (m_lirs_type.get(key) == 'LIR')

    if key in m_lirs_S:
        del m_lirs_S[key]
        m_lirs_S[key] = None
        if is_lir:
            prune_lirs_S()
        else:
            m_lirs_type[key] = 'LIR'
            m_lirs_count += 1
            if key in m_lirs_Q: del m_lirs_Q[key]
            if m_lirs_count > m_lirs_limit:
                demote_bottom_lir()
    else:
        m_lirs_S[key] = None
        m_lirs_type[key] = 'HIR'
        if key in m_lirs_Q: del m_lirs_Q[key]
        m_lirs_Q[key] = None

    # Stack S size control
    stack_limit = cache_snapshot.capacity * 3
    if len(m_lirs_S) > stack_limit:
        k = next(iter(m_lirs_S))
        del m_lirs_S[k]
        if m_lirs_type.get(k) == 'LIR':
            m_lirs_type[k] = 'HIR'
            m_lirs_count -= 1
            if k not in m_lirs_Q: m_lirs_Q[k] = None
        else:
            if k not in m_lirs_Q and k in m_lirs_type: del m_lirs_type[k]
        prune_lirs_S()

def evict(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global m_lirs_limit, m_lirs_Q, m_lirs_count

    m_lirs_limit = max(1, int(cache_snapshot.capacity * 0.99))

    while m_lirs_count > m_lirs_limit:
        demote_bottom_lir()

    if not m_lirs_Q:
        demote_bottom_lir()

    victim_key = None
    # Find first resident HIR
    for k in m_lirs_Q:
        if k in cache_snapshot.cache:
            victim_key = k
            break

    if not victim_key:
         victim_key = next(iter(cache_snapshot.cache))

    return victim_key

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    access_lirs(cache_snapshot, obj.key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    access_lirs(cache_snapshot, obj.key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    check_reset(cache_snapshot)
    global m_lirs_Q
    if evicted_obj.key in m_lirs_Q:
        del m_lirs_Q[evicted_obj.key]

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