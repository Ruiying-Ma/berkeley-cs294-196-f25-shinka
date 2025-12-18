# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

# Segmented LRU (2Q-style) metadata:
# - m_key_timestamp: last access time for LRU within segments
# - m_tier: 'A1' (probation; seen once) or 'Am' (protected; seen >=2 times)
# - m_freq: hit count (used for tie-breaking on eviction)
m_key_timestamp = dict()
m_tier = dict()
m_freq = dict()

def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global m_key_timestamp, m_tier, m_freq
    keys = list(cache_snapshot.cache.keys())
    if not keys:
        return None

    # Segment the current cache keys
    a1_keys = [k for k in keys if m_tier.get(k, 'A1') == 'A1']
    am_keys = [k for k in keys if m_tier.get(k) == 'Am']

    total = len(keys)
    target_a1 = max(1, total // 4)  # keep ~25% in probation

    # Decide which segment to evict from
    if not a1_keys and not am_keys:
        pick_from = keys
    elif not a1_keys:
        pick_from = am_keys
    elif not am_keys:
        pick_from = a1_keys
    elif len(a1_keys) > target_a1:
        pick_from = a1_keys
    else:
        # Prefer evicting from A1 to protect reusable items;
        # if A1 is small, evict from Am.
        pick_from = a1_keys if a1_keys else am_keys

    # Choose LRU within the chosen segment; tie-break by lowest frequency
    def ts(k): return m_key_timestamp.get(k, -1)
    if not pick_from:
        pick_from = keys
    min_ts = min(ts(k) for k in pick_from)
    ts_candidates = [k for k in pick_from if ts(k) == min_ts]
    if len(ts_candidates) > 1:
        min_f = min(m_freq.get(k, 1) for k in ts_candidates)
        freq_candidates = [k for k in ts_candidates if m_freq.get(k, 1) == min_f]
        candid_obj_key = freq_candidates[0]
    else:
        candid_obj_key = ts_candidates[0]
    return candid_obj_key

def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq
    # Update recency and frequency
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_freq[obj.key] = m_freq.get(obj.key, 0) + 1

    # Promote to protected segment on first hit (or if metadata missing)
    if m_tier.get(obj.key) != 'Am':
        m_tier[obj.key] = 'Am'

def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq
    # New items start in probation (A1)
    m_key_timestamp[obj.key] = cache_snapshot.access_count
    m_tier[obj.key] = 'A1'
    # Initialize frequency to 1 on insertion
    m_freq[obj.key] = m_freq.get(obj.key, 0) + 1 if obj.key in m_freq else 1

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp, m_tier, m_freq
    # Clean up all metadata for the evicted key
    m_key_timestamp.pop(evicted_obj.key, None)
    m_tier.pop(evicted_obj.key, None)
    m_freq.pop(evicted_obj.key, None)

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