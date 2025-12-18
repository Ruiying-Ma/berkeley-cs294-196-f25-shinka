# EVOLVE-BLOCK-START
"""
S3-FIFO with Ghost-Gated Admission and Frequency Scaling (S3-FIFO-G-Gate).
Implements actionable recommendations:
1. Ghost-Gated Admission: When the small queue is full, new items not in Ghost are "rejected" (placed in a temporary queue to be evicted first). This effectively filters scan traffic (Trace 14).
2. Extended Ghost: Capacity increased to 5x to support the gate and capture longer loops.
3. Increased Frequency: Cap raised to 7 to better differentiate hot items.
4. Reduced Small Queue: Target reduced to 5% to maximize protected space for Main.
"""

from collections import OrderedDict

# Global structures
# _q_small: FIFO queue for probation (5% target)
# _q_main: FIFO queue for protected items (95% target)
# _q_ghost: FIFO queue for tracking history (5x capacity)
# _q_rejected: FIFO queue for items bypassing admission (eviction candidates)
# _freq_map: Dictionary mapping key -> frequency count (0-7)
_q_small = OrderedDict()
_q_main = OrderedDict()
_q_ghost = OrderedDict()
_q_rejected = OrderedDict()
_freq_map = {}
_last_access_count = -1

def _reset_state_if_needed(snapshot):
    """Resets internal state if a new trace is detected."""
    global _q_small, _q_main, _q_ghost, _q_rejected, _freq_map, _last_access_count
    if snapshot.access_count < _last_access_count:
        _q_small.clear()
        _q_main.clear()
        _q_ghost.clear()
        _q_rejected.clear()
        _freq_map.clear()
    _last_access_count = snapshot.access_count

def evict(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _q_rejected, _freq_map
    _reset_state_if_needed(cache_snapshot)

    capacity = cache_snapshot.capacity
    # Target size for small queue (5% of capacity)
    s_target = max(1, int(capacity * 0.05))

    # Safety fallback
    if not _q_small and not _q_main and not _q_rejected:
        return next(iter(cache_snapshot.cache)) if cache_snapshot.cache else None

    while True:
        # 0. Check Rejected Queue (The "Gate" for scans)
        # Items here were denied entry to Small. Evict them first unless hit.
        if _q_rejected:
            candidate, _ = _q_rejected.popitem(last=False)

            if candidate not in cache_snapshot.cache:
                _freq_map.pop(candidate, None)
                continue

            freq = _freq_map.get(candidate, 0)
            if freq > 0:
                # Surprise hit on rejected item -> Promote to Main
                _q_main[candidate] = None
                _freq_map[candidate] = 0
                continue
            else:
                # Evict
                # Ensure it's in Ghost (it should be from insertion, but refresh/ensure)
                if candidate in _q_ghost:
                    _q_ghost.move_to_end(candidate)
                else:
                    _q_ghost[candidate] = None

                # Manage Ghost Capacity (5x)
                while len(_q_ghost) > capacity * 5:
                    _q_ghost.popitem(last=False)

                _freq_map.pop(candidate, None)
                return candidate

        # 1. Check Small Queue (Probation)
        if len(_q_small) > s_target or not _q_main:
            if not _q_small:
                if _q_main:
                    candidate, _ = _q_main.popitem(last=False)
                    _freq_map.pop(candidate, None)
                    return candidate
                return next(iter(cache_snapshot.cache))

            candidate, _ = _q_small.popitem(last=False)

            if candidate not in cache_snapshot.cache:
                _freq_map.pop(candidate, None)
                continue

            freq = _freq_map.get(candidate, 0)
            if freq > 0:
                # Promotion: Small -> Main
                _q_main[candidate] = None
                _freq_map[candidate] = 0
                continue
            else:
                # Eviction: Small -> Ghost
                _q_ghost[candidate] = None
                while len(_q_ghost) > capacity * 5:
                    _q_ghost.popitem(last=False)
                _freq_map.pop(candidate, None)
                return candidate

        # 2. Check Main Queue (Protected)
        else:
            candidate, _ = _q_main.popitem(last=False)

            if candidate not in cache_snapshot.cache:
                _freq_map.pop(candidate, None)
                continue

            freq = _freq_map.get(candidate, 0)
            if freq > 0:
                # Reinsertion: Main -> Main (Second Chance with Decay)
                _q_main[candidate] = None
                _freq_map[candidate] = freq - 1
                continue
            else:
                # Conditional Demotion
                if len(_q_small) < s_target:
                    _q_small[candidate] = None
                    _freq_map[candidate] = 0
                    continue
                else:
                    # Eviction: Main -> Ghost
                    _q_ghost[candidate] = None
                    while len(_q_ghost) > capacity * 5:
                        _q_ghost.popitem(last=False)
                    _freq_map.pop(candidate, None)
                    return candidate

def update_after_hit(cache_snapshot, obj):
    global _freq_map
    _reset_state_if_needed(cache_snapshot)

    # Increased Frequency Ceiling to 7
    curr = _freq_map.get(obj.key, 0)
    _freq_map[obj.key] = min(curr + 1, 7)

def update_after_insert(cache_snapshot, obj):
    global _q_small, _q_main, _q_ghost, _q_rejected, _freq_map
    _reset_state_if_needed(cache_snapshot)

    key = obj.key
    capacity = cache_snapshot.capacity
    s_target = max(1, int(capacity * 0.05))

    if key in _q_ghost:
        # Rescue: Ghost -> Main
        del _q_ghost[key]
        _q_main[key] = None
        _freq_map[key] = 0
    else:
        # Ghost-Gated Admission
        if len(_q_small) >= s_target:
            # Reject: Add to Rejected Queue (evicted ASAP)
            _q_rejected[key] = None
            # Add to Ghost immediately
            _q_ghost[key] = None
            while len(_q_ghost) > capacity * 5:
                _q_ghost.popitem(last=False)
            _freq_map[key] = 0
        else:
            # Admit to Small
            _q_small[key] = None
            _freq_map[key] = 0

def update_after_evict(cache_snapshot, obj, evicted_obj):
    global _q_small, _q_main, _q_rejected, _freq_map
    _reset_state_if_needed(cache_snapshot)

    key = evicted_obj.key
    if key in _q_small: del _q_small[key]
    if key in _q_main: del _q_main[key]
    if key in _q_rejected: del _q_rejected[key]
    _freq_map.pop(key, None)
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