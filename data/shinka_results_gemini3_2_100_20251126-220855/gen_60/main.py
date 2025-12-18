# EVOLVE-BLOCK-START
"""Cache eviction algorithm: S3-LIFO-LRU with Ghost-Frequency Restoration"""
from collections import OrderedDict

# Global State
s3_small = OrderedDict() # Small queue (Probationary, LIFO eviction)
s3_main = OrderedDict()  # Main queue (Protected, LRU eviction)
s3_ghost = OrderedDict() # Ghost registry (FIFO, stores key -> freq)
s3_freq = {}             # Frequency map
s3_config = {}           # Configuration parameters
last_op_count = -1

def _reset_state(cache_snapshot):
    global s3_small, s3_main, s3_ghost, s3_freq, s3_config, last_op_count
    if cache_snapshot.access_count < last_op_count:
        s3_small.clear()
        s3_main.clear()
        s3_ghost.clear()
        s3_freq.clear()
        s3_config.clear()
    last_op_count = cache_snapshot.access_count

    if not s3_config:
        cap = cache_snapshot.capacity
        # Small queue size target (10%)
        s3_config['small_target'] = max(1, int(cap * 0.1))
        # Aging interval (once per capacity accesses)
        s3_config['aging_interval'] = cap
        # Ghost registry size target (8x capacity for better loop detection)
        s3_config['ghost_target'] = cap * 8

def _age_freqs():
    # Halve all frequencies
    for k in list(s3_freq):
        s3_freq[k] >>= 1
        if s3_freq[k] == 0:
            del s3_freq[k]

def evict(cache_snapshot, obj):
    '''
    S3-LIFO-LRU Eviction with Anti-Thrashing and Second Chance:
    - Ages frequencies periodically.
    - Favors evicting from Small (Probationary) using LIFO with probabilistic FIFO leak.
    - Promotes from Small to Main if freq > 0 (at least one hit).
    - Evicts from Main (Protected) using LRU with Second Chance.
    '''
    _reset_state(cache_snapshot)

    # Aging Logic
    if cache_snapshot.access_count % s3_config['aging_interval'] == 0:
        _age_freqs()

    target_small = s3_config['small_target']

    # 1. Try evicting from Small if it's over budget or if Main is empty
    while len(s3_small) > target_small or (not s3_main and s3_small):
        # Anti-Thrashing: Probabilistic FIFO leak (approx 3%)
        # Allows some new items to traverse the probationary queue instead of immediate LIFO eviction.
        is_fifo_leak = (cache_snapshot.access_count & 0x1F) == 0

        if is_fifo_leak:
            victim_key, _ = s3_small.popitem(last=False) # FIFO (oldest)
        else:
            victim_key, _ = s3_small.popitem(last=True)  # LIFO (newest)

        # Adaptive Promotion: Compare with Main's LRU victim
        victim_freq = s3_freq.get(victim_key, 0)
        should_promote = False

        if not s3_main:
            should_promote = True
        else:
            # Peek at Main's LRU without removing
            main_lru_key = next(iter(s3_main))
            main_lru_freq = s3_freq.get(main_lru_key, 0)
            # Promote if we are at least as valuable as the item we would eventually evict from Main
            if victim_freq >= main_lru_freq:
                should_promote = True

        if should_promote:
            # Promote to Main (MRU)
            s3_main[victim_key] = 1
            s3_main.move_to_end(victim_key)
        else:
            # Victim found. Put back to ensure consistency for update_after_evict
            s3_small[victim_key] = 1
            return victim_key

    # 2. If Small is safe, evict from Main (LRU with Second Chance)
    if s3_main:
        # Give a Second Chance to high-frequency items in LRU position
        # Check up to 4 candidates to avoid deep scans
        for _ in range(4):
            victim_key = next(iter(s3_main))
            freq = s3_freq.get(victim_key, 0)

            # If freq is decent (e.g. >= 2), give second chance
            if freq >= 2:
                s3_main.move_to_end(victim_key) # Move to MRU
                s3_freq[victim_key] >>= 1       # Decay frequency
            else:
                return victim_key

        # Fallback if all candidates had high freq
        return next(iter(s3_main))

    # Fallback (should be covered)
    if s3_small:
        return next(iter(s3_small))
    return None

def update_after_hit(cache_snapshot, obj):
    '''
    On Hit:
    - Main: Move to MRU (Strict LRU).
    - Small: Increment freq (Lazy promotion).
    '''
    _reset_state(cache_snapshot)
    key = obj.key

    if key in s3_main:
        s3_main.move_to_end(key)
    elif key in s3_small:
        s3_freq[key] = s3_freq.get(key, 0) + 1

def update_after_insert(cache_snapshot, obj):
    '''
    On Insert:
    - Ghost Hit: Restore freq (decayed), Promote to Main.
    - New: Insert to Small (Probation).
    '''
    _reset_state(cache_snapshot)
    key = obj.key

    if key in s3_ghost:
        # Restore frequency with decay
        restored_freq = s3_ghost.pop(key)
        s3_freq[key] = max(0, restored_freq // 2)
        # Insert to Small (Probation)
        s3_small[key] = 1
    else:
        # New Item -> Small
        s3_small[key] = 1
        s3_freq[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    On Evict:
    - Remove from queues.
    - Save to Ghost (with frequency).
    '''
    key = evicted_obj.key
    capacity = cache_snapshot.capacity

    if key in s3_small:
        del s3_small[key]
    elif key in s3_main:
        del s3_main[key]

    # Save to Ghost
    current_freq = s3_freq.get(key, 0)
    s3_ghost[key] = current_freq
    s3_ghost.move_to_end(key) # Mark as recent in Ghost

    # Clean up freq map
    if key in s3_freq:
        del s3_freq[key]

    # Maintain Ghost size
    ghost_target = s3_config.get('ghost_target', capacity)
    if len(s3_ghost) > ghost_target:
        s3_ghost.popitem(last=False) # Remove oldest ghost
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