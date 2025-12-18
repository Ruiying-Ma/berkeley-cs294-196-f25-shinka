# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

import random
import math

# LeCaR State
algo_state = {
    'freq': {},          # key -> frequency (int)
    'last_access': {},   # key -> last access timestamp (int)
    'h_lru': {},         # key -> dummy (history of keys evicted by LRU)
    'h_lfu': {},         # key -> dummy (history of keys evicted by LFU)
    'w': 0.5,            # Probability of using LRU eviction
    'learning_rate': 0.2,
    'max_time': 0,       # Track time to detect trace resets
    'last_choice': None  # 'LRU', 'LFU', or 'BOTH'
}

def _check_reset(current_time):
    # If time goes backwards, we are likely processing a new trace
    if current_time < algo_state['max_time']:
        algo_state['freq'].clear()
        algo_state['last_access'].clear()
        algo_state['h_lru'].clear()
        algo_state['h_lfu'].clear()
        algo_state['w'] = 0.5
        algo_state['max_time'] = 0
        algo_state['last_choice'] = None
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    LeCaR Eviction:
    Choose the item to evict based on a weighted probability between LRU and LFU policies.
    '''
    cache_keys = list(cache_snapshot.cache.keys())
    if not cache_keys:
        return None

    # Find candidates for LRU (min last_access) and LFU (min freq)
    victim_lru = None
    min_la = float('inf')

    victim_lfu = None
    min_freq = float('inf')

    freq_map = algo_state['freq']
    la_map = algo_state['last_access']

    # Single pass to find both candidates
    for key in cache_keys:
        la = la_map.get(key, 0)
        f = freq_map.get(key, 1)

        if la < min_la:
            min_la = la
            victim_lru = key

        if f < min_freq:
            min_freq = f
            victim_lfu = key
        elif f == min_freq:
            # Tie-break LFU with LRU
            if la < la_map.get(victim_lfu, 0):
                victim_lfu = key

    # Decision
    if victim_lru == victim_lfu:
        algo_state['last_choice'] = 'BOTH'
        return victim_lru

    # Randomized choice
    if random.random() < algo_state['w']:
        algo_state['last_choice'] = 'LRU'
        return victim_lru
    else:
        algo_state['last_choice'] = 'LFU'
        return victim_lfu

def update_after_hit(cache_snapshot, obj):
    '''
    Hit: Update frequency and last access time.
    '''
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    algo_state['freq'][key] = algo_state['freq'].get(key, 0) + 1
    algo_state['last_access'][key] = cache_snapshot.access_count

def update_after_insert(cache_snapshot, obj):
    '''
    Insert: Initialize metadata and update weights based on history (learning).
    '''
    _check_reset(cache_snapshot.access_count)
    key = obj.key
    curr_time = cache_snapshot.access_count

    # Initialize
    algo_state['freq'][key] = 1
    algo_state['last_access'][key] = curr_time

    # Learning
    w = algo_state['w']
    lr = algo_state['learning_rate']

    in_h_lru = key in algo_state['h_lru']
    in_h_lfu = key in algo_state['h_lfu']

    if in_h_lru:
        # Mistake by LRU -> Decrease W (prob of LRU)
        algo_state['w'] = w * math.exp(-lr)
        del algo_state['h_lru'][key]

    if in_h_lfu:
        # Mistake by LFU -> Increase W (prob of LRU)
        algo_state['w'] = 1.0 - (1.0 - w) * math.exp(-lr)
        del algo_state['h_lfu'][key]

    # Cap W to avoid getting stuck
    algo_state['w'] = max(0.001, min(0.999, algo_state['w']))

    # Manage history size (limit to capacity)
    capacity = cache_snapshot.capacity
    if len(algo_state['h_lru']) > capacity:
        # Remove oldest inserted (approx by iteration order in Python 3.7+)
        algo_state['h_lru'].pop(next(iter(algo_state['h_lru'])))

    if len(algo_state['h_lfu']) > capacity:
        algo_state['h_lfu'].pop(next(iter(algo_state['h_lfu'])))

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict: Add victim to appropriate history.
    '''
    key = evicted_obj.key
    policy = algo_state.get('last_choice')

    if policy == 'LRU':
        algo_state['h_lru'][key] = True
    elif policy == 'LFU':
        algo_state['h_lfu'][key] = True
    elif policy == 'BOTH':
        algo_state['h_lru'][key] = True
        algo_state['h_lfu'][key] = True

    # Clean up
    if key in algo_state['freq']:
        del algo_state['freq'][key]
    if key in algo_state['last_access']:
        del algo_state['last_access'][key]

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