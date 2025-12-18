# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import deque

# S3-FIFO State
algo_state = {
    'S': deque(),        # Small FIFO queue (approx 10% cache)
    'M': deque(),        # Main FIFO queue (approx 90% cache)
    'ghost': {},         # Ghost cache (dict for FIFO ordering)
    'accessed': set(),   # Track accessed items (1-bit clock)
    'location': {},      # Map key -> 'S' or 'M'
    'max_time': 0        # Track time to detect trace resets
}

def _check_reset(current_time):
    # If time goes backwards, we are likely processing a new trace
    if current_time < algo_state['max_time']:
        algo_state['S'].clear()
        algo_state['M'].clear()
        algo_state['ghost'].clear()
        algo_state['accessed'].clear()
        algo_state['location'].clear()
        algo_state['max_time'] = 0
    algo_state['max_time'] = current_time

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Logic:
    - Maintains a small FIFO (S) and a main FIFO (M).
    - New items go to S.
    - Eviction candidates are chosen from S (if full) or M.
    - Items in S with access bit set move to M.
    - Items in M with access bit set get reinserted (Second Chance).
    '''
    S = algo_state['S']
    M = algo_state['M']
    accessed = algo_state['accessed']
    location = algo_state['location']

    capacity = cache_snapshot.capacity
    target_S_size = max(1, int(capacity * 0.1))

    victim = None

    # Loop until a victim is found.
    # This loop modifies the queues by moving "saved" items.
    while victim is None:
        # Determine which queue to evict from
        evict_from_S = False
        if len(S) > target_S_size:
            evict_from_S = True
        elif len(M) == 0:
            evict_from_S = True

        if evict_from_S:
            if not S: break # Should not happen if cache not empty

            candidate = S[0] # Peek head (oldest)
            if candidate in accessed:
                # Been accessed: Promote to M
                S.popleft()
                M.append(candidate)
                location[candidate] = 'M'
                accessed.remove(candidate)
            else:
                # Not accessed: Evict
                victim = candidate
        else:
            if not M: break

            candidate = M[0] # Peek head (oldest)
            if candidate in accessed:
                # Been accessed: Reinsert in M (Second Chance)
                M.popleft()
                M.append(candidate)
                # location is already 'M'
                accessed.remove(candidate)
            else:
                # Not accessed: Evict
                victim = candidate

    return victim

def update_after_hit(cache_snapshot, obj):
    '''
    Hit: Mark object as accessed.
    '''
    _check_reset(cache_snapshot.access_count)
    algo_state['accessed'].add(obj.key)

def update_after_insert(cache_snapshot, obj):
    '''
    Insert: Place in S or M (if ghost).
    '''
    _check_reset(cache_snapshot.access_count)
    key = obj.key

    # If previously evicted from M (ghost), restore to M
    if key in algo_state['ghost']:
        algo_state['M'].append(key)
        algo_state['location'][key] = 'M'
        del algo_state['ghost'][key]
    else:
        # New item goes to S
        algo_state['S'].append(key)
        algo_state['location'][key] = 'S'

    # Start with accessed bit 0 (unless it was a hit/insert race, but usually 0)
    # We explicitly remove it to be safe, though usually not present.
    if key in algo_state['accessed']:
        algo_state['accessed'].remove(key)

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    Evict: Remove victim from queues and manage ghost.
    '''
    key = evicted_obj.key
    loc = algo_state['location'].get(key)

    # Remove from S or M
    if loc == 'S':
        # Optimize: usually at head
        if algo_state['S'] and algo_state['S'][0] == key:
            algo_state['S'].popleft()
        else:
            try: algo_state['S'].remove(key)
            except ValueError: pass
        # Evicted from S -> No ghost (scan filter)

    elif loc == 'M':
        if algo_state['M'] and algo_state['M'][0] == key:
            algo_state['M'].popleft()
        else:
            try: algo_state['M'].remove(key)
            except ValueError: pass

        # Evicted from M -> Add to ghost
        algo_state['ghost'][key] = True

        # Maintain ghost size <= capacity
        if len(algo_state['ghost']) > cache_snapshot.capacity:
            # FIFO eviction from ghost
            oldest = next(iter(algo_state['ghost']))
            del algo_state['ghost'][oldest]

    # Cleanup metadata
    if key in algo_state['location']:
        del algo_state['location'][key]
    if key in algo_state['accessed']:
        algo_state['accessed'].remove(key)

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