# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

m_key_timestamp = dict()
m_key_frequency = dict()
m_key_priority = dict()
g_L = 0.0

def evict(cache_snapshot, obj):
    '''
    GDSF-like eviction: Score = L + Frequency / Size.
    Evicts the object with the lowest score.
    Updates L to the score of the evicted object.
    Tie-breaker: LRU.
    '''
    global g_L

    min_key = None
    min_priority = float('inf')
    min_ts = float('inf')

    # Candidate selection: Find object with min priority
    for key, item in cache_snapshot.cache.items():
        # Ensure priority exists (recover if missing)
        if key not in m_key_priority:
            freq = m_key_frequency.get(key, 1)
            size = item.size if item.size > 0 else 1
            m_key_priority[key] = g_L + (freq / size)

        p = m_key_priority[key]
        ts = m_key_timestamp.get(key, 0)

        if p < min_priority:
            min_key = key
            min_priority = p
            min_ts = ts
        elif p == min_priority:
            # Tie-breaker: LRU (smallest timestamp)
            if ts < min_ts:
                min_key = key
                min_ts = ts

    # Update L to the priority of the evicted object
    if min_key is not None:
        g_L = min_priority

    return min_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after hit: update timestamp, freq, priority.
    '''
    global m_key_timestamp, m_key_frequency, m_key_priority, g_L
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_key_frequency[obj.key] = m_key_frequency.get(obj.key, 0) + 1

    freq = m_key_frequency[obj.key]
    size = obj.size if obj.size > 0 else 1
    # GDSF update: H = L + F/S
    m_key_priority[obj.key] = g_L + (freq / size)

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after insert: set timestamp, init/update freq, priority.
    '''
    global m_key_timestamp, m_key_frequency, m_key_priority, g_L
    m_key_timestamp[obj.key] = cache_snapshot.access_count

    # Ghost frequency
    if obj.key in m_key_frequency:
        m_key_frequency[obj.key] += 1
    else:
        m_key_frequency[obj.key] = 1

    freq = m_key_frequency[obj.key]
    size = obj.size if obj.size > 0 else 1
    # GDSF update
    m_key_priority[obj.key] = g_L + (freq / size)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after evict: remove timestamp/priority, decay frequency.
    '''
    global m_key_timestamp, m_key_priority, m_key_frequency
    if evicted_obj.key in m_key_timestamp:
        m_key_timestamp.pop(evicted_obj.key)
    if evicted_obj.key in m_key_priority:
        m_key_priority.pop(evicted_obj.key)

    # Decay frequency in history
    if evicted_obj.key in m_key_frequency:
        m_key_frequency[evicted_obj.key] //= 2

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