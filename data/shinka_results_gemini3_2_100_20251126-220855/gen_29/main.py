# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""
from collections import OrderedDict
import random

# Adaptive Probationary LRU (AP-LRU) Global State
ap_small = OrderedDict()       # Small/Probation Queue (FIFO-like with random eviction)
ap_main = OrderedDict()        # Main/Protected Queue (LRU)
ap_ghost_s = OrderedDict()     # Ghost of Small
ap_ghost_m = OrderedDict()     # Ghost of Main
ap_freq = {}                   # Access bits/Frequency
ap_ratio = 0.1                 # Target fraction for Small Queue (0.0 to 1.0)
m_last_access_count = -1

def _check_reset(cache_snapshot):
    """Resets global state if a new trace is detected."""
    global ap_small, ap_main, ap_ghost_s, ap_ghost_m, ap_freq, ap_ratio, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        ap_small.clear()
        ap_main.clear()
        ap_ghost_s.clear()
        ap_ghost_m.clear()
        ap_freq.clear()
        ap_ratio = 0.1
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    Adaptive Probationary LRU Eviction.
    - Adapts size of Small vs Main using Ghost hits.
    - Evicts from Small (Probation) using randomized window to break loops.
    - Promotes from Small to Main on hit.
    - Evicts from Main (Protected) using strict LRU.
    '''
    _check_reset(cache_snapshot)
    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * ap_ratio))
    
    # Eviction loop to handle internal promotions
    while True:
        # Determine source queue
        # Prefer evicting from Small if it exceeds target size OR if Main is empty
        if len(ap_small) > target_small or not ap_main:
            # --- Evict from Small (Probation) ---
            if not ap_small:
                # Should not happen if cache is full and Main is empty
                return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

            # Look at a window of candidates at the head of Small
            # Increasing window slightly to 10 for better randomization against loops
            window_size = 10
            candidates = []
            iterator = iter(ap_small)
            for _ in range(window_size):
                try:
                    candidates.append(next(iterator))
                except StopIteration:
                    break
            
            # 1. Check for promotions (Lazy Promotion)
            promoted_key = None
            for key in candidates:
                if ap_freq.get(key, 0) > 0:
                    promoted_key = key
                    break
            
            if promoted_key:
                # Promote to Main (MRU)
                del ap_small[promoted_key]
                ap_main[promoted_key] = 1 # Insert at tail (MRU)
                ap_freq[promoted_key] = 0 # Reset hit bit
                # Loop continues to find next victim
                continue
            
            # 2. No promotions -> Select victim
            # Randomized selection from candidates
            victim_key = random.choice(candidates)
            return victim_key

        else:
            # --- Evict from Main (Protected) ---
            # Strict LRU: Evict the head of Main
            victim_key = next(iter(ap_main))
            return victim_key

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - If in Small: Mark as accessed (freq=1).
    - If in Main: Move to MRU (LRU update).
    '''
    _check_reset(cache_snapshot)
    key = obj.key
    
    if key in ap_small:
        ap_freq[key] = 1
    elif key in ap_main:
        ap_main.move_to_end(key) # Update LRU position

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Check Ghosts to adapt ratio.
    - Insert to Small or Main.
    '''
    _check_reset(cache_snapshot)
    global ap_ratio
    key = obj.key
    delta = 0.05 # Adaptation step
    
    # Adaptation Logic
    if key in ap_ghost_s:
        # Hit in Ghost Small -> Small was too small
        ap_ratio = min(0.9, ap_ratio + delta)
        del ap_ghost_s[key]
        # Recall to Main (proven utility)
        ap_main[key] = 1
        ap_freq[key] = 0
    elif key in ap_ghost_m:
        # Hit in Ghost Main -> Main was too small
        ap_ratio = max(0.05, ap_ratio - delta)
        del ap_ghost_m[key]
        # Recall to Main
        ap_main[key] = 1
        ap_freq[key] = 0
    else:
        # Brand new -> Small
        ap_small[key] = 1
        ap_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict: Move to Ghost queues.
    '''
    key = evicted_obj.key
    
    if key in ap_small:
        del ap_small[key]
        ap_ghost_s[key] = 1
    elif key in ap_main:
        del ap_main[key]
        ap_ghost_m[key] = 1
        
    if key in ap_freq:
        del ap_freq[key]
        
    # Manage Ghost Capacity (Total Ghost size ~= Cache Capacity)
    # To keep history balanced, limit each ghost to roughly capacity? 
    # Or total? Let's limit total to 1x Capacity for simplicity and memory safety.
    max_ghost = cache_snapshot.capacity
    while len(ap_ghost_s) + len(ap_ghost_m) > max_ghost:
        # Evict FIFO from ghosts. Which one?
        # Remove from the larger one, or just FIFO?
        # Let's remove from the one corresponding to the current ratio roughly, 
        # or simply remove oldest.
        # Since OrderedDict is insertion order, we can pop first.
        # Simple policy: Remove from the larger ghost list.
        if len(ap_ghost_s) > len(ap_ghost_m):
            ap_ghost_s.popitem(last=False)
        elif ap_ghost_m:
             ap_ghost_m.popitem(last=False)
        elif ap_ghost_s:
             ap_ghost_s.popitem(last=False)
        else:
            break
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