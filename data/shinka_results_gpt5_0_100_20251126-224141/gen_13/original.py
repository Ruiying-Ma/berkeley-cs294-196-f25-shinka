# EVOLVE-BLOCK-START
"""Cache eviction algorithm for optimizing hit rates across multiple workloads"""

from collections import OrderedDict, defaultdict
from itertools import islice

# LRU timestamp map kept for compatibility and as a tie-breaker
m_key_timestamp = dict()

# Lightweight TinyLFU-style frequency sketch
m_freq = defaultdict(int)
m_decay_last = 0  # last access_count when decay happened
# Track last access_count when we incremented frequency to avoid double counting per access
m_last_freq_inc_access = -1

# Adaptive Replacement Cache (ARC) metadata
arc_T1 = OrderedDict()  # recent, resident
arc_T2 = OrderedDict()  # frequent, resident
arc_B1 = OrderedDict()  # ghost of T1
arc_B2 = OrderedDict()  # ghost of T2
arc_p = 0               # target size of T1
arc_capacity = None     # will be initialized from cache_snapshot
arc_last_adj = -1       # prevent double p-adjustment within same access


def _ensure_capacity(cache_snapshot):
    global arc_capacity
    if arc_capacity is None:
        arc_capacity = max(int(cache_snapshot.capacity), 1)

def _maybe_decay(cache_snapshot):
    # Periodically decay frequencies to keep counts bounded
    global m_decay_last
    # decay every ~10k accesses
    if cache_snapshot.access_count - m_decay_last >= 10000:
        for k in list(m_freq.keys()):
            cnt = m_freq[k] >> 1
            if cnt <= 0:
                # keep small footprint
                m_freq.pop(k, None)
            else:
                m_freq[k] = cnt
        m_decay_last = cache_snapshot.access_count


def _move_to_mru(od, key):
    # Push key to MRU position of an OrderedDict
    if key in od:
        od.pop(key, None)
    od[key] = True


def _pop_lru(od):
    if od:
        k, _ = od.popitem(last=False)
        return k
    return None


def _trim_ghosts():
    # Keep ghosts total size within capacity
    total = len(arc_B1) + len(arc_B2)
    cap = arc_capacity if arc_capacity is not None else 1
    while total > cap:
        # Evict from the larger ghost list first
        if len(arc_B1) >= len(arc_B2):
            _pop_lru(arc_B1)
        else:
            _pop_lru(arc_B2)
        total = len(arc_B1) + len(arc_B2)


def _resync(cache_snapshot):
    # Ensure resident metadata tracks actual cache content
    cache_keys = set(cache_snapshot.cache.keys())
    for k in list(arc_T1.keys()):
        if k not in cache_keys:
            arc_T1.pop(k, None)
    for k in list(arc_T2.keys()):
        if k not in cache_keys:
            arc_T2.pop(k, None)
    # Add any cached keys we missed to T1 as recent
    for k in cache_keys:
        if k not in arc_T1 and k not in arc_T2:
            arc_T1[k] = True
    _trim_ghosts()


def evict(cache_snapshot, obj):
    '''
    This function defines how the algorithm chooses the eviction victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The new object that needs to be inserted into the cache.
    - Return:
        - `candid_obj_key`: The key of the cached object that will be evicted to make room for `obj`.
    '''
    global arc_p, arc_last_adj, m_last_freq_inc_access
    _ensure_capacity(cache_snapshot)
    _resync(cache_snapshot)
    _maybe_decay(cache_snapshot)

    # Count this access for the requested object once (TinyLFU-like)
    if m_last_freq_inc_access != cache_snapshot.access_count:
        m_freq[obj.key] += 1
        m_last_freq_inc_access = cache_snapshot.access_count

    # Adjust ARC target p once per access based on ghost membership
    if arc_last_adj != cache_snapshot.access_count:
        if obj.key in arc_B1:
            inc = max(1, len(arc_B2) // max(1, len(arc_B1)))
            arc_p = min(arc_capacity, arc_p + inc)
            arc_last_adj = cache_snapshot.access_count
        elif obj.key in arc_B2:
            dec = max(1, len(arc_B1) // max(1, len(arc_B2)))
            arc_p = max(0, arc_p - dec)
            arc_last_adj = cache_snapshot.access_count

    # ARC preference between T1 and T2 for replacement
    x_in_B2 = obj.key in arc_B2
    t1_sz = len(arc_T1)
    prefer_t1 = (t1_sz >= 1 and (t1_sz > arc_p or (x_in_B2 and t1_sz == arc_p)))

    def _freq(k):
        return m_freq.get(k, 0)

    # Sample a few LRU candidates from each segment and pick least frequent
    S = 3  # small sample size to limit overhead
    keys_t1 = list(islice(arc_T1.keys(), 0, S))
    keys_t2 = list(islice(arc_T2.keys(), 0, S))

    def pick_best(keys):
        if not keys:
            return None, None
        best = None
        best_f = None
        best_ts = None
        for k in keys:
            f = _freq(k)
            ts = m_key_timestamp.get(k, float('inf'))
            if best is None or f < best_f or (f == best_f and ts <= best_ts):
                best = k
                best_f = f
                best_ts = ts
        return best, best_f

    b1, f1 = pick_best(keys_t1)
    b2, f2 = pick_best(keys_t2)

    # Fallback if both lists are empty (shouldn't happen often)
    if b1 is None and b2 is None:
        candidate = None
        if m_key_timestamp:
            min_ts = min(m_key_timestamp.get(k, float('inf')) for k in cache_snapshot.cache.keys())
            for k in cache_snapshot.cache.keys():
                if m_key_timestamp.get(k, float('inf')) == min_ts:
                    candidate = k
                    break
        if candidate is None and cache_snapshot.cache:
            candidate = next(iter(cache_snapshot.cache.keys()))
        return candidate

    if b1 is None:
        return b2
    if b2 is None:
        return b1

    # Choose segment per ARC, but override if alternate is much less frequent
    if prefer_t1:
        cand, c_f = b1, f1
        alt, a_f = b2, f2
    else:
        cand, c_f = b2, f2
        alt, a_f = b1, f1

    # If alternate frequency is significantly smaller, prefer evicting from alternate
    # Criterion: a_f*2 + 1 < c_f (avoid evicting a very hot item)
    if a_f is not None and c_f is not None and (a_f * 2 + 1) < c_f:
        return alt

    # Otherwise, break ties on frequency with timestamp
    if c_f == a_f:
        ts_c = m_key_timestamp.get(cand, float('inf'))
        ts_a = m_key_timestamp.get(alt, float('inf'))
        return cand if ts_c <= ts_a else alt
    else:
        return cand


def update_after_hit(cache_snapshot, obj):
    '''
    This function defines how the algorithm update the metadata it maintains immediately after a cache hit.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object accessed during the cache hit.
    - Return: `None`
    '''
    global m_key_timestamp, m_last_freq_inc_access
    _ensure_capacity(cache_snapshot)
    _maybe_decay(cache_snapshot)
    # ARC: on hit, move to T2 MRU
    key = obj.key
    if key in arc_T1:
        arc_T1.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # If already in T2, refresh; if not present due to drift, place in T2
        if key in arc_T2:
            _move_to_mru(arc_T2, key)
        else:
            _move_to_mru(arc_T2, key)
    # Update frequency once per access and timestamp for tie-breaking/fallback
    if m_last_freq_inc_access != cache_snapshot.access_count:
        m_freq[key] += 1
        m_last_freq_inc_access = cache_snapshot.access_count
    m_key_timestamp[key] = cache_snapshot.access_count


def update_after_insert(cache_snapshot, obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after inserting a new object into the cache.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object that was just inserted into the cache.
    - Return: `None`
    '''
    global m_key_timestamp, arc_p, arc_last_adj, m_last_freq_inc_access
    _ensure_capacity(cache_snapshot)
    _maybe_decay(cache_snapshot)
    key = obj.key
    # ARC admission policy (also handles non-full cache case).
    # Avoid double adjustment within the same access if already done in evict.
    if arc_last_adj != cache_snapshot.access_count and key in arc_B1:
        # Previously evicted from T1: favor recency by increasing p
        inc = max(1, len(arc_B2) // max(1, len(arc_B1)))
        arc_p = min(arc_capacity, arc_p + inc)
        arc_last_adj = cache_snapshot.access_count
        arc_B1.pop(key, None)
        _move_to_mru(arc_T2, key)
    elif arc_last_adj != cache_snapshot.access_count and key in arc_B2:
        # Previously frequent: favor frequency by decreasing p
        dec = max(1, len(arc_B1) // max(1, len(arc_B2)))
        arc_p = max(0, arc_p - dec)
        arc_last_adj = cache_snapshot.access_count
        arc_B2.pop(key, None)
        _move_to_mru(arc_T2, key)
    else:
        # Brand new: insert into T1 (recent)
        _move_to_mru(arc_T1, key)
    _trim_ghosts()
    # Count this access if it wasn't already counted in evict (non-full cache case)
    if m_last_freq_inc_access != cache_snapshot.access_count:
        m_freq[key] += 1
        m_last_freq_inc_access = cache_snapshot.access_count
    m_key_timestamp[key] = cache_snapshot.access_count


def update_after_evict(cache_snapshot, obj, evicted_obj):
    '''
    This function defines how the algorithm updates the metadata it maintains immediately after evicting the victim.
    - Args:
        - `cache_snapshot`: A snapshot of the current cache state.
        - `obj`: The object to be inserted into the cache.
        - `evicted_obj`: The object that was just evicted from the cache.
    - Return: `None`
    '''
    global m_key_timestamp
    _ensure_capacity(cache_snapshot)
    k = evicted_obj.key
    # Move evicted resident to corresponding ghost list
    if k in arc_T1:
        arc_T1.pop(k, None)
        _move_to_mru(arc_B1, k)
    elif k in arc_T2:
        arc_T2.pop(k, None)
        _move_to_mru(arc_B2, k)
    else:
        # Unknown membership: default to B1
        _move_to_mru(arc_B1, k)
    # Remove timestamp entry for evicted item to avoid growth
    m_key_timestamp.pop(k, None)
    _trim_ghosts()

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