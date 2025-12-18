# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict

# W-TinyLFU inspired State
algo_state = {
    'window': OrderedDict(),    # Window Cache (SLRU or LRU)
    'probation': OrderedDict(), # Main Cache - Probation (SLRU)
    'protected': OrderedDict(), # Main Cache - Protected (SLRU)
    'freq': {},                 # Frequency Counter
    'doorkeeper': OrderedDict(),# Doorkeeper filter (Ordered for LRU)
    'freq_count': 0,            # Total increments for reset
    'max_time': 0
}

def _check_reset(current_time):
    if current_time < algo_state['max_time']:
        algo_state['window'].clear()
        algo_state['probation'].clear()
        algo_state['protected'].clear()
        algo_state['freq'].clear()
        algo_state['doorkeeper'].clear()
        algo_state['freq_count'] = 0
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    W-TinyLFU Eviction Logic with Incumbency Bias and Adaptive Window
    '''
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']

    capacity = cache_snapshot.capacity
    w_cap = max(1, int(capacity * 0.05)) # 5% Window

    # Candidates
    candidate_w = next(iter(window)) if window else None

    # Main Candidate: Probation LRU, else Protected LRU
    candidate_m = None
    if probation:
        candidate_m = next(iter(probation))
    elif protected:
        candidate_m = next(iter(protected))

    victim = None

    fw = freq.get(candidate_w, 0) if candidate_w else 0
    fm = freq.get(candidate_m, 0) if candidate_m else 0

    # 1. Adapt Window size
    # Only evict Main to grow Window if Main candidate is NOT valuable (freq=0)
    # If Main candidate has some frequency, we prefer to Duel.
    if len(window) < w_cap and candidate_m and fm == 0:
        victim = candidate_m
    elif not candidate_m:
        victim = candidate_w
    elif not candidate_w:
        victim = candidate_m
    else:
        # 2. Duel
        # Bias towards Main (Incumbent). Window must be strictly better.
        # Hysteresis: Candidate W must have freq > Main + 1
        if fw > fm + 1:
            victim = candidate_m
        else:
            victim = candidate_w

    return victim

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']
    dk = algo_state['doorkeeper']

    # Frequency Update
    if key in freq:
        freq[key] = min(freq[key] + 1, 15) # Saturation at 15
    elif key in dk:
        freq[key] = 1
        del dk[key] # Remove from DK once promoted to freq
    else:
        dk[key] = None # Insert into DK
        # Maintain DK size (Decoupled from aging)
        if len(dk) > cache_snapshot.capacity * 5:
            dk.popitem(last=False)

    algo_state['freq_count'] += 1
    # Extended Aging Interval (10x capacity)
    if algo_state['freq_count'] >= cache_snapshot.capacity * 10:
        to_remove = []
        # Do NOT clear Doorkeeper here (Recommendation 2)
        for k in freq:
            freq[k] //= 2
            if freq[k] == 0: to_remove.append(k)
        for k in to_remove: del freq[k]
        algo_state['freq_count'] = 0

    # Cache Maintenance
    if key in window:
        window.move_to_end(key)
    elif key in protected:
        protected.move_to_end(key)
    elif key in probation:
        # Promote
        del probation[key]
        protected[key] = None

        # Enforce Protected Limit (80% of Main)
        w_cap = max(1, int(cache_snapshot.capacity * 0.05))
        m_cap = cache_snapshot.capacity - w_cap
        p_cap = int(m_cap * 0.8)

        while len(protected) > p_cap:
            k, _ = protected.popitem(last=False)
            probation[k] = None # Move to Probation MRU
            probation.move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    freq = algo_state['freq']
    dk = algo_state['doorkeeper']

    # Frequency Update
    if key in freq:
        freq[key] = min(freq[key] + 1, 15)
    elif key in dk:
        freq[key] = 1
        del dk[key]
    else:
        dk[key] = None
        if len(dk) > cache_snapshot.capacity * 5:
            dk.popitem(last=False)

    algo_state['freq_count'] += 1
    # Aging
    if algo_state['freq_count'] >= cache_snapshot.capacity * 10:
        to_remove = []
        for k in freq:
            freq[k] //= 2
            if freq[k] == 0: to_remove.append(k)
        for k in to_remove: del freq[k]
        algo_state['freq_count'] = 0

    # Insert to Window MRU
    algo_state['window'][key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    v_key = evicted_obj.key
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']

    if v_key in window:
        del window[v_key]
    elif v_key in probation:
        del probation[v_key]
    elif v_key in protected:
        del protected[v_key]

    # Check for Window promotion
    # If we evicted Main, and Window is now > Cap (it wasn't evicted), move Window LRU to Probation
    w_cap = max(1, int(cache_snapshot.capacity * 0.05))
    if len(window) > w_cap:
        k, _ = window.popitem(last=False)
        probation[k] = None # To Probation MRU
        probation.move_to_end(k)

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