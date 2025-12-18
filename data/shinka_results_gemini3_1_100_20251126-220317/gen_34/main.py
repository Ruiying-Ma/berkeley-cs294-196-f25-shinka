# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import deque, OrderedDict

# W-TinyLFU style state with Doorkeeper
algo_state = {
    'window': deque(),          # Queue of (key, size)
    'window_size': 0,           # Current size of window
    'probation': OrderedDict(), # key -> size
    'protected': OrderedDict(), # key -> size
    'protected_size': 0,
    'freq': {},                 # key -> count
    'doorkeeper': set(),        # Set for 1-hit wonders
    'access_count': 0
}

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction:
    - Maintains a small Window (1%) and a Main SLRU (Probation + Protected).
    - If Window is not full, evict from Main (to allow Window to grow).
    - If Window is full, duel the Window victim vs Main victim using frequency.
    '''
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']
    doorkeeper = algo_state['doorkeeper']

    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.01))

    # 1. If Window has space, prefer evicting from Main (Probation -> Protected)
    if algo_state['window_size'] < w_cap:
        if probation:
            return next(iter(probation))
        if protected:
            return next(iter(protected))
        if window:
            return window[0][0]

    # 2. Window is full. Find candidates.
    if not window:
        # Fallback if window empty (rare)
        if probation: return next(iter(probation))
        if protected: return next(iter(protected))
        return None

    cand_w_key = window[0][0]

    if probation:
        cand_m_key = next(iter(probation))
    elif protected:
        cand_m_key = next(iter(protected))
    else:
        return cand_w_key

    # Duel: Keep the one with higher frequency
    def get_freq(k):
        if k in freq: return freq[k]
        if k in doorkeeper: return 1
        return 0

    freq_w = get_freq(cand_w_key)
    freq_m = get_freq(cand_m_key)

    if freq_w > freq_m:
        return cand_m_key
    else:
        return cand_w_key

def _update_freq(key, capacity):
    algo_state['access_count'] += 1
    freq = algo_state['freq']
    doorkeeper = algo_state['doorkeeper']

    # Aging
    if algo_state['access_count'] >= capacity:
        algo_state['access_count'] = 0
        doorkeeper.clear()
        # Filter and halve
        new_freq = {}
        for k, v in freq.items():
            nv = v // 2
            if nv > 0:
                new_freq[k] = nv
        algo_state['freq'] = new_freq
        freq = new_freq # Update local reference

    # Update
    if key in freq:
        freq[key] = min(freq[key] + 1, 15)
    elif key in doorkeeper:
        freq[key] = 2
        doorkeeper.remove(key)
    else:
        doorkeeper.add(key)

def update_after_hit(cache_snapshot, obj):
    '''
    Update frequency and manage SLRU promotion.
    '''
    key = obj.key
    _update_freq(key, cache_snapshot.capacity)

    # SLRU Management
    if key in algo_state['protected']:
        algo_state['protected'].move_to_end(key)
    elif key in algo_state['probation']:
        # Promote from Probation to Protected
        val = algo_state['probation'].pop(key)
        algo_state['protected'][key] = val
        algo_state['protected_size'] += val

        # Enforce Protected Limit (80% of capacity)
        limit = int(cache_snapshot.capacity * 0.8)
        while algo_state['protected_size'] > limit and algo_state['protected']:
            k, v = algo_state['protected'].popitem(last=False)
            algo_state['protected_size'] -= v
            algo_state['probation'][k] = v

def update_after_insert(cache_snapshot, obj):
    '''
    Insert new object into Window and handle overflow to Probation.
    '''
    key = obj.key
    size = obj.size
    _update_freq(key, cache_snapshot.capacity)

    # Insert into Window
    algo_state['window'].append((key, size))
    algo_state['window_size'] += size

    # Check Window Overflow -> Move to Probation
    w_cap = max(1, int(cache_snapshot.capacity * 0.01))
    while algo_state['window_size'] > w_cap and algo_state['window']:
        k, s = algo_state['window'].popleft()
        algo_state['window_size'] -= s
        algo_state['probation'][k] = s

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Remove evicted object from internal structures.
    '''
    key = evicted_obj.key
    size = evicted_obj.size

    # Check and remove from locations
    if algo_state['window'] and algo_state['window'][0][0] == key:
        algo_state['window'].popleft()
        algo_state['window_size'] -= size
    elif key in algo_state['probation']:
        del algo_state['probation'][key]
    elif key in algo_state['protected']:
        val = algo_state['protected'].pop(key)
        algo_state['protected_size'] -= val

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