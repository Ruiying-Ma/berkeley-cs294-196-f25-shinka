# EVOLVE-BLOCK-START
"""S3-FIFO with Epsilon-Greedy Eviction and Extended Ghost History"""
from collections import OrderedDict
import random
import itertools

# Global State
# s3e_small: FIFO queue for probationary items
# s3e_main: LRU queue for protected items
# s3e_ghost: Ghost queue for tracking items evicted from Small
# s3e_freq: Frequency counters
s3e_small = OrderedDict()
s3e_main = OrderedDict()
s3e_ghost = OrderedDict()
s3e_freq = {}
s3e_ratio = 0.1
s3e_access_count_check = 0

def check_reset(cache_snapshot):
    global s3e_small, s3e_main, s3e_ghost, s3e_freq, s3e_ratio, s3e_access_count_check
    if cache_snapshot.access_count < s3e_access_count_check:
        s3e_small.clear()
        s3e_main.clear()
        s3e_ghost.clear()
        s3e_freq.clear()
        s3e_ratio = 0.1
    s3e_access_count_check = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    '''
    S3-FIFO with Epsilon-Greedy Eviction.
    - Epsilon-Greedy: Randomly evicts from Small queue (5% prob) to break loops.
    - Extended Ghost: Tracks history up to 2x capacity to catch longer loops.
    - Adaptive Sizing: Adjusts Small/Main ratio based on Ghost hits.
    - Strict Promotion: Moves items to Main only if accessed during probation.
    '''
    check_reset(cache_snapshot)
    global s3e_small, s3e_main, s3e_freq, s3e_ratio

    capacity = cache_snapshot.capacity
    target_small = max(1, int(capacity * s3e_ratio))

    # Extended Ghost cleanup (2x capacity to catch larger loops)
    while len(s3e_ghost) > 2 * capacity:
        s3e_ghost.popitem(last=False)

    while True:
        # Determine whether to evict from Small or Main
        # Prefer Small if it exceeds target size or if Main is empty
        process_small = (len(s3e_small) > target_small) or (not s3e_main)

        if process_small:
            if not s3e_small:
                # Fallback to Main if Small is empty
                if s3e_main:
                    process_small = False
                else:
                    return None 

        if process_small:
            # --- Epsilon-Greedy Eviction ---
            # Randomly pick a victim from a small window at the head of Small
            # This introduces noise to desynchronize from cache-thrashing loops
            victim_key = None
            
            # 5% chance to pick randomly from the first 20 items
            if len(s3e_small) > 10 and random.random() < 0.05:
                k = min(len(s3e_small), 20)
                window = list(itertools.islice(s3e_small, k))
                victim_key = random.choice(window)
            else:
                # Standard FIFO Head
                victim_key = next(iter(s3e_small))

            # Check promotion criteria
            freq = s3e_freq.get(victim_key, 0)
            if freq > 0:
                # Hit in Small -> Promote to Main
                if victim_key in s3e_small:
                    del s3e_small[victim_key]
                    s3e_main[victim_key] = None
                    s3e_freq[victim_key] = 0 # Reset frequency
                continue
            else:
                # Evict
                return victim_key

        else:
            # --- Main Queue Eviction (LRU with Second Chance) ---
            candidate = next(iter(s3e_main))
            freq = s3e_freq.get(candidate, 0)
            
            if freq > 0:
                # Second Chance: Reinsert at MRU
                s3e_main.move_to_end(candidate)
                s3e_freq[candidate] = max(0, freq - 1)
                continue
            else:
                return candidate

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global s3e_freq, s3e_main
    key = obj.key
    # Increment frequency, cap at 3
    s3e_freq[key] = min(s3e_freq.get(key, 0) + 1, 3)
    
    # If in Main, update LRU position
    if key in s3e_main:
        s3e_main.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global s3e_small, s3e_main, s3e_ghost, s3e_freq, s3e_ratio
    
    key = obj.key
    s3e_freq[key] = 0 # Initialize freq
    
    # Adaptive Sizing based on Ghost Hits
    delta = 0.02
    
    if key in s3e_ghost:
        # Ghost Hit: Item was evicted from Small but accessed again.
        # This implies Small was too small. Increase Small ratio.
        s3e_ratio = min(0.9, s3e_ratio + delta)
        
        # Rescue item to Main (it has proven value)
        s3e_main[key] = None
        del s3e_ghost[key]
    else:
        # New insert -> Small
        s3e_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    check_reset(cache_snapshot)
    global s3e_small, s3e_main, s3e_ghost, s3e_freq
    
    key = evicted_obj.key
    
    # Track evicted Small items in Ghost
    if key in s3e_small:
        del s3e_small[key]
        s3e_ghost[key] = None
    elif key in s3e_main:
        del s3e_main[key]
        
    if key in s3e_freq:
        del s3e_freq[key]
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