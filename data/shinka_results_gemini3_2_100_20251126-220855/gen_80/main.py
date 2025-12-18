# EVOLVE-BLOCK-START
"""S3-FIFO with Multi-bit Frequency Counters and Jittered LRU"""
import random

# S3-FIFO Metadata
# s3_small: FIFO queue for the small segment (probation)
# s3_main: LRU queue for the main segment (protected)
# s3_ghost: Ghost cache for tracking eviction from small
# s3_freq: Frequency counter for objects (max 3)
s3_small = {}
s3_main = {}
s3_ghost = {}
s3_freq = {}

def evict(cache_snapshot, obj):
    '''
    S3-FIFO Eviction Policy with Multi-bit Clock and Jittered LRU:
    - Small Queue (S): FIFO-like with Windowed Scan. Hits promote to Main.
    - Main Queue (M): LRU + Frequency.
    - Ghost Queue (G): Tracks items evicted from S. Capacity = 3x cache.
    - Windowed Eviction: Checks first k items in Small to find promotions or random victim.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    import itertools

    capacity = cache_snapshot.capacity
    # Jittered Small Capacity
    noise_range = max(1, int(capacity * 0.01))
    s_capacity = max(1, int(capacity * 0.1) + random.randint(-noise_range, noise_range))

    # Extended Ghost Registry (3x capacity)
    # Allows detecting larger loops or longer reuse intervals
    while len(s3_ghost) > 3 * capacity:
        s3_ghost.pop(next(iter(s3_ghost)))

    while True:
        # Decision: Evict from Small or Main?
        if len(s3_small) >= s_capacity or not s3_main:
            if not s3_small:
                return None

            # Windowed Scan in Small (size 5)
            # Find any candidate with freq > 0 to promote
            window = list(itertools.islice(s3_small, 5))
            promoted_key = None
            for candidate in window:
                if s3_freq.get(candidate, 0) > 0:
                    promoted_key = candidate
                    break

            if promoted_key:
                # Promote to Main
                s3_small.pop(promoted_key)
                s3_main[promoted_key] = None
                s3_freq[promoted_key] = 0 # Reset frequency on promotion
                continue

            # No promotions found in window.
            # Pick a RANDOM victim from the window to break loops (e.g. Trace 14).
            victim = random.choice(window)
            return victim

        else:
            # Evict from Main (Strict LRU with Frequency Check)
            candidate = next(iter(s3_main))
            freq = s3_freq.get(candidate, 0)

            if freq > 0:
                # Reinsert to MRU with decay
                s3_main.pop(candidate)
                s3_main[candidate] = None
                s3_freq[candidate] = freq - 1
                continue
            else:
                return candidate

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Increment frequency, capped at 3.
    - If in Main, move to MRU (True LRU).
    '''
    global s3_freq, s3_main
    key = obj.key
    s3_freq[key] = min(s3_freq.get(key, 0) + 1, 3)

    if key in s3_main:
        # Move to MRU to maintain LRU property
        val = s3_main.pop(key)
        s3_main[key] = val

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - If in Ghost (Rescue):
        - Move to Main.
        - Restore frequency from Ghost (decayed by half).
    - Else (New):
        - Insert to Small.
        - Initialize frequency to 0.
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = obj.key

    if key in s3_ghost:
        # Rescue to Main
        s3_main[key] = None
        # Restore frequency: decayed by half
        # Handle None if present from older runs
        val = s3_ghost.pop(key)
        restored_freq = (val if val is not None else 0) // 2
        s3_freq[key] = restored_freq
    else:
        # New insert to Small
        s3_small[key] = None
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - If evicted from Small: move to Ghost and store current frequency.
    - If evicted from Main: remove completely.
    - Remove from s3_freq (store in Ghost if needed).
    '''
    global s3_small, s3_main, s3_ghost, s3_freq
    key = evicted_obj.key

    if key in s3_small:
        s3_small.pop(key)
        # Store frequency in ghost for potential restoration
        s3_ghost[key] = s3_freq.get(key, 0)
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