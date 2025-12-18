# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict
import random

# Adaptive S3-FIFO Global State
as3_small = OrderedDict()       # Small Queue (FIFO) for new items
as3_main = OrderedDict()        # Main Queue (FIFO/LRU) for promoted items
as3_ghost_small = OrderedDict() # Ghost for Small
as3_ghost_main = OrderedDict()  # Ghost for Main
as3_freq = {}                   # Frequency counter
as3_ratio = 0.1                 # Target fraction for Small Queue (0.01 to 0.99)
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global as3_small, as3_main, as3_ghost_small, as3_ghost_main, as3_freq, as3_ratio, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        as3_small.clear()
        as3_main.clear()
        as3_ghost_small.clear()
        as3_ghost_main.clear()
        as3_freq.clear()
        as3_ratio = 0.1
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Adaptive S3-FIFO Eviction.
    - S (Small) buffers new items. M (Main) holds frequent items.
    - Dynamic sizing of S vs M based on ghost hits (ARC-like adaptivity).
    - Randomized victim selection from head window (scan/loop resistance).
    '''
    _check_reset(cache_snapshot)
    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * as3_ratio))
    
    # Window size for randomized selection
    k_window = 10 

    while True:
        # 1. Determine eviction source queue
        # Evict from Small if it exceeds target size OR if Main is empty
        if len(as3_small) > target_small or not as3_main:
            queue = as3_small
            is_small = True
        else:
            queue = as3_main
            is_small = False
        
        # 2. Collect candidates from head of queue
        candidates = []
        iterator = iter(queue)
        for _ in range(k_window):
            try:
                candidates.append(next(iterator))
            except StopIteration:
                break
        
        if not candidates:
            # Fallback (should not happen if cache is full)
            return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

        # 3. Check for Promotions / Reinsertions
        promoted = False
        for key in candidates:
            freq = as3_freq.get(key, 0)
            if freq > 0:
                # Promotion/Reinsertion logic
                if is_small:
                    # Promote Small -> Main
                    del as3_small[key]
                    as3_main[key] = 1
                else:
                    # Reinsert Main -> Main (Tail)
                    as3_main.move_to_end(key)
                
                as3_freq[key] = 0 # Reset frequency
                promoted = True
                break # Restart loop
        
        if promoted:
            continue

        # 4. No promotions in window -> Evict one
        # Randomized selection from candidates breaks synchronization
        victim_key = random.choice(candidates)
        return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit: Increment frequency.
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    as3_freq[key] = min(3, as3_freq.get(key, 0) + 1)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghost lists to adapt S/M ratio.
    - Insert into Small or Main.
    '''
    _check_reset(cache_snapshot)
    global as3_ratio
    key = obj.key
    
    delta = 0.05 # Adaptation step size

    if key in as3_ghost_small:
        # Hit in Ghost Small -> S was too small
        as3_ratio = min(0.95, as3_ratio + delta)
        del as3_ghost_small[key]
        # Recall to Main
        as3_main[key] = 1
        as3_freq[key] = 0
    elif key in as3_ghost_main:
        # Hit in Ghost Main -> M was too small (S too big)
        as3_ratio = max(0.05, as3_ratio - delta)
        del as3_ghost_main[key]
        # Recall to Main
        as3_main[key] = 1
        as3_freq[key] = 0
    else:
        # New Insert -> Small
        as3_small[key] = 1
        as3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Remove from queue and add to respective Ghost.
    '''
    key = evicted_obj.key
    
    if key in as3_small:
        del as3_small[key]
        as3_ghost_small[key] = 1
    elif key in as3_main:
        del as3_main[key]
        as3_ghost_main[key] = 1
    
    if key in as3_freq:
        del as3_freq[key]

    # Limit Ghost Size to Capacity
    capacity = cache_snapshot.capacity
    if len(as3_ghost_small) > capacity:
        as3_ghost_small.popitem(last=False)
    if len(as3_ghost_main) > capacity:
        as3_ghost_main.popitem(last=False)

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