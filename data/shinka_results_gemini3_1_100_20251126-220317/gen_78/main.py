# EVOLVE-BLOCK-START
"""Cache eviction algorithm optimizing for hit rate using W-TinyLFU concepts"""
from collections import OrderedDict

# Global State
algo_state = {
    'window': OrderedDict(),    # Window Cache (LRU) - Admission buffer
    'probation': OrderedDict(), # Main Cache - Probation (SLRU segment)
    'protected': OrderedDict(), # Main Cache - Protected (SLRU segment)
    'freq': {},                 # Frequency Counter (Sketch approximation)
    'doorkeeper': set(),        # 1-bit counter filter
    'access_count': 0,          # Internal counter for aging
    'max_time': 0,              # Track time to detect resets
}

def _reset_state():
    algo_state['window'].clear()
    algo_state['probation'].clear()
    algo_state['protected'].clear()
    algo_state['freq'].clear()
    algo_state['doorkeeper'].clear()
    algo_state['access_count'] = 0
    algo_state['max_time'] = 0

def _check_reset(current_time):
    # Detect if trace restarted or time jumped backwards
    if current_time < algo_state['max_time']:
        _reset_state()
    algo_state['max_time'] = current_time

def _update_freq(key, capacity):
    freq = algo_state['freq']
    dk = algo_state['doorkeeper']
    
    # Update Frequency
    if key in freq:
        freq[key] = min(freq[key] + 1, 15) # Saturation at 15
    elif key in dk:
        freq[key] = 1
        dk.remove(key)
    else:
        dk.add(key)
    
    # Doorkeeper Reset Management - Size based
    if len(dk) > capacity * 2:
        dk.clear()
        
    algo_state['access_count'] += 1
    
    # Periodic Aging of Frequency
    # Aging every 10x capacity to preserve history longer
    if algo_state['access_count'] >= capacity * 10:
        # Halve frequencies
        to_remove = []
        for k, v in freq.items():
            freq[k] = v // 2
            if freq[k] == 0:
                to_remove.append(k)
        for k in to_remove:
            del freq[k]
        algo_state['access_count'] = 0

def evict(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    freq = algo_state['freq']
    
    capacity = cache_snapshot.capacity
    # Configuration: 1% Window, rest Main
    w_cap = max(1, int(capacity * 0.01))
    
    # Candidates
    candidate_w = next(iter(window)) if window else None
    
    # Candidate from Main (Probation LRU preferred, then Protected LRU)
    candidate_m = None
    if probation:
        candidate_m = next(iter(probation))
    elif protected:
        candidate_m = next(iter(protected))
        
    # Logic
    # 1. If Window needs to grow (fill up to 1%)
    if len(window) < w_cap:
        if candidate_m:
            return candidate_m
        else:
            return candidate_w # Fallback (only if Main empty)
            
    # 2. Window is full. Duel.
    if not candidate_w: return candidate_m 
    if not candidate_m: return candidate_w
    
    # Compare frequencies
    fw = freq.get(candidate_w, 0)
    fm = freq.get(candidate_m, 0)
    
    # Tie-breaker: Main wins (Scan resistance)
    if fw > fm:
        return candidate_m # Evict Main, Admit Window
    else:
        return candidate_w # Evict Window, Reject Admission

def update_after_hit(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    _update_freq(key, cache_snapshot.capacity)
    
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    
    if key in window:
        window.move_to_end(key)
    elif key in protected:
        protected.move_to_end(key)
    elif key in probation:
        # Promotion: Probation -> Protected
        del probation[key]
        protected[key] = None # Insert MRU
        
        # Balance Main Cache Segments
        # Protected target: 80% of Main Capacity
        w_cap = max(1, int(cache_snapshot.capacity * 0.01))
        main_cap = cache_snapshot.capacity - w_cap
        p_cap = int(main_cap * 0.8)
        
        while len(protected) > p_cap:
            # Demote Protected LRU -> Probation MRU
            k, _ = protected.popitem(last=False)
            probation[k] = None
            probation.move_to_end(k)

def update_after_insert(cache_snapshot, obj):
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    _update_freq(key, cache_snapshot.capacity)
    
    # Always insert into Window MRU
    algo_state['window'][key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    v_key = evicted_obj.key
    window = algo_state['window']
    probation = algo_state['probation']
    protected = algo_state['protected']
    
    # Determine where the victim was and remove it
    victim_was_in_main = False
    if v_key in window:
        del window[v_key]
    elif v_key in probation:
        del probation[v_key]
        victim_was_in_main = True
    elif v_key in protected:
        del protected[v_key]
        victim_was_in_main = True
        
    # Maintain Window Size constraint & Handle Admission
    # If we evicted Main, it means Window won the duel (or we are growing Window).
    # We must move Window LRU to Probation to keep Window size stable (at cap)
    # allowing the new insert to take the spot in Window.
    w_cap = max(1, int(cache_snapshot.capacity * 0.01))
    
    # If victim was Main, and Window is full/overflowing, promote Window LRU
    if victim_was_in_main and len(window) >= w_cap:
        k, _ = window.popitem(last=False)
        probation[k] = None
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