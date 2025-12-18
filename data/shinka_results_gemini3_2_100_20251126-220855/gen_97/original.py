# EVOLVE-BLOCK-START
from collections import OrderedDict

# S3-FIFO with:
# - LIFO T1 (Scan Resistance)
# - T2 with Frequency-based Second Chance
# - Capped Frequency and Aging
# - Extended Ghost History

m_t1 = OrderedDict()
m_t2 = OrderedDict()
m_b1 = OrderedDict()
m_p = 0.0
m_last_access_count = 0

MAX_FREQ = 3

def check_reset(cache_snapshot):
    global m_t1, m_t2, m_b1, m_p, m_last_access_count
    if cache_snapshot.access_count < m_last_access_count:
        m_t1.clear()
        m_t2.clear()
        m_b1.clear()
        m_p = 0.0
    m_last_access_count = cache_snapshot.access_count

def evict(cache_snapshot, obj):
    check_reset(cache_snapshot)
    global m_p

    capacity = cache_snapshot.capacity
    current_time = cache_snapshot.access_count

    # 1. Global Frequency Aging
    # Occurs every 2 * capacity accesses
    if current_time - m_p > capacity * 2:
        # Decay frequencies
        for k in m_b1:
            m_b1[k] //= 2
        m_p = float(current_time)

    # T1 Target Size (10%)
    t1_target = int(capacity * 0.1)
    if t1_target < 1: t1_target = 1

    # 2. Evict from T1 (Probation)
    # Use LIFO to handle scans (scan items are evicted immediately)
    if len(m_t1) >= t1_target:
        while len(m_t1) > 0:
            victim_key = next(reversed(m_t1)) # LIFO Candidate

            # Promotion Check:
            # If freq > 1 (meaning seen before, e.g., in ghost), promote to T2
            if m_b1.get(victim_key, 0) > 1:
                del m_t1[victim_key]
                m_t2[victim_key] = None # Move to T2 MRU

                # If T1 is now small enough, stop
                if len(m_t1) < t1_target:
                    break
            else:
                # Evict victim (Failed probation)
                return victim_key

    # 3. Evict from T2 (Main) - Second Chance
    # Search for a victim with low frequency
    # Limit search to avoid O(N) in worst case
    search_limit = 10
    while m_t2:
        candidate = next(iter(m_t2)) # LRU Candidate

        freq = m_b1.get(candidate, 0)

        # If freq > 0, give second chance (retain)
        if freq > 0 and search_limit > 0:
            m_t2.move_to_end(candidate) # Reinsert at MRU
            m_b1[candidate] = freq - 1  # Decrement frequency (Second Chance cost)
            search_limit -= 1
        else:
            # Evict
            return candidate

    # Fallback (should be rare if T2 has items)
    if m_t1:
        return next(reversed(m_t1))
    return obj.key

def update_after_hit(cache_snapshot, obj):
    check_reset(cache_snapshot)
    key = obj.key
    # Increment frequency, capped at MAX_FREQ
    m_b1[key] = min(m_b1.get(key, 0) + 1, MAX_FREQ)
    m_b1.move_to_end(key) # Keep LRU order for ghost

    if key in m_t1:
        # Promote on hit if freq criteria met
        if m_b1[key] > 1:
            del m_t1[key]
            m_t2[key] = None
    elif key in m_t2:
        m_t2.move_to_end(key)

def update_after_insert(cache_snapshot, obj):
    check_reset(cache_snapshot)
    key = obj.key
    # Insert counts as access. Restore/Increment frequency.
    # If key was in ghost, it retains partial frequency (minus aging).
    m_b1[key] = min(m_b1.get(key, 0) + 1, MAX_FREQ)
    m_b1.move_to_end(key)

    # Always insert into T1 (Probation)
    m_t1[key] = None

def update_after_evict(cache_snapshot, obj, evicted_obj):
    check_reset(cache_snapshot)
    key = evicted_obj.key

    if key in m_t1:
        del m_t1[key]
    elif key in m_t2:
        del m_t2[key]

    # Limit Ghost Size
    # Keep up to 5x capacity to capture larger loops
    if len(m_b1) > cache_snapshot.capacity * 5:
        m_b1.popitem(last=False) # Remove oldest

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