# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

m_key_timestamp = dict()
m_key_frequency = dict()

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    Score = Frequency / (Age * sqrt(Size)).
    Evicts the object with the lowest score.
    Tie-breaker: LRU (Oldest).
    '''
    current_time = cache_snapshot.access_count

    # We want to minimize (freq / (age * sqrt(size)))
    # Comparison: F1/(A1*sqrt(S1)) < F2/(A2*sqrt(S2))
    # Square both sides: F1^2 / (A1^2 * S1) < F2^2 / (A2^2 * S2)
    # Cross-multiply: F1^2 * A2^2 * S2 < F2^2 * A1^2 * S1

    min_key = None
    min_freq = -1
    min_age = -1
    min_size = -1

    for key, item in cache_snapshot.cache.items():
        if key not in m_key_timestamp: continue

        freq = m_key_frequency.get(key, 0)
        last_access = m_key_timestamp[key]
        age = current_time - last_access
        if age <= 0: age = 1

        size = item.size
        if size <= 0: size = 1

        if min_key is None:
            min_key = key
            min_freq = freq
            min_age = age
            min_size = size
        else:
            # Compare curr < min
            # (curr_freq^2 * min_age^2 * min_size) < (min_freq^2 * curr_age^2 * curr_size)

            lhs = (freq * freq) * (min_age * min_age) * min_size
            rhs = (min_freq * min_freq) * (age * age) * size

            if lhs < rhs:
                min_key = key
                min_freq = freq
                min_age = age
                min_size = size
            elif lhs == rhs:
                # Tie-breaker: LRU (oldest timestamp => largest age)
                if age > min_age:
                    min_key = key
                    min_freq = freq
                    min_age = age
                    min_size = size

    return min_key

def update_after_hit(cache_snapshot, obj):
    '''
    Update metadata after hit: update timestamp and increment frequency.
    '''
    global m_key_timestamp, m_key_frequency
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_key_frequency[obj.key] = m_key_frequency.get(obj.key, 0) + 1

def update_after_insert(cache_snapshot, obj):
    '''
    Update metadata after insert: set timestamp.
    Init frequency to 1 (or increment ghost) to give new items a chance.
    '''
    global m_key_timestamp, m_key_frequency
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_key_frequency[obj.key] = m_key_frequency.get(obj.key, 0) + 1

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Update metadata after evict: remove timestamp.
    Decay frequency in ghost history to prevent infinite accumulation.
    '''
    global m_key_timestamp, m_key_frequency
    if evicted_obj.key in m_key_timestamp:
        m_key_timestamp.pop(evicted_obj.key)

    # Decay frequency
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