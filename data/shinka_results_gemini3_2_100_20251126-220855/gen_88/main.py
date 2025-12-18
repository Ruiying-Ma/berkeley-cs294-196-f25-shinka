# EVOLVE-BLOCK-START
"""S3-FIFO with Clock Main, Extended Ghost, and Scan Resistance"""
import random

# Global State
# s3_small: FIFO queue for the small segment (probation)
# s3_main: FIFO queue for the main segment (protected, managed as Clock)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}

def evict(cache_snapshot, obj):
    '''
    S3-FIFO with Clock Main, Extended Ghost, and Scan Resistance.
    
    Improvements:
    1.  **Clock-based Main Segment**: Instead of LRU (Move-to-MRU), we use a FIFO queue with 
        Second Chance (Clock). Hits only increment frequency. Eviction checks frequency 
        at the head; if > 0, it decrements and reinserts at the tail. This improves 
        scan resistance and handling of looping patterns.
    2.  **Extended Ghost Registry**: Maintains 3x capacity history to catch larger loops.
    3.  **Probabilistic Promotion**: Uses a deterministic hash-based trigger to promote 
        a small percentage (1%) of scan items to Main, anchoring parts of working sets 
        larger than the cache.
    4.  **Jittered Small Capacity**: Dynamically adjusts the Small segment size to prevent 
        resonance with loop sizes.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq

    capacity = cache_snapshot.capacity
    
    # Jittered Target for Small Queue (10% +/- 1%)
    # Use deterministic noise based on access count to allow reproducibility and stability
    jitter = max(1, int(capacity * 0.01))
    mod_jitter = (cache_snapshot.access_count % (2 * jitter + 1)) - jitter
    s_capacity = max(1, int(capacity * 0.1) + mod_jitter)

    # 1. Extended Ghost Cleanup (3x Capacity)
    # Keeping a longer history allows rescuing items from loops slightly larger than cache
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # 2. Decision: Evict from Small or Main?
        # Evict from Small if it exceeds its dynamic target size, or if Main is empty.
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                # Should not happen in a full cache unless empty
                return None

            candidate = next(iter(s3_small))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Promotion: Merit-based (Hit in Small)
                # Lazy promotion: we only promote when the item reaches the eviction point
                s3_small.pop(candidate)
                s3_main[candidate] = None # Insert at tail of Main
                s3_freq[candidate] = 0    # Reset freq to require new proof of utility in Main
                continue
            
            # Probabilistic Promotion (1% chance)
            # Helps anchor items from large loops/scans that otherwise thrash
            if (cache_snapshot.access_count ^ hash(candidate)) % 100 == 0:
                s3_small.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = 0
                continue

            # Evict from Small
            return candidate

        else:
            # 3. Evict from Main (Clock Policy)
            # FIFO traversal. If freq > 0, give second chance.
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Second Chance: Reinsert at tail, decrement freq
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            
            # Evict from Main
            return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Increment frequency (cap at 3).
    - Do NOT move to MRU. Preserves FIFO order for Clock algorithm.
    '''
    global s3_freq
    s3_freq[obj.key] = min(s3_freq.get(obj.key, 0) + 1, 3)

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost: Rescue to Main.
    - Else: Insert to Small.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key
    s3_freq[key] = 0

    if key in s3_ghost:
        s3_main[key] = None
        s3_ghost.pop(key)
    else:
        s3_small[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Small -> Ghost.
    - Main -> Gone.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        s3_ghost[key] = None
    elif key in s3_main:
        s3_main.pop(key)

    if key in s3_freq:
        s3_freq.pop(key)
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