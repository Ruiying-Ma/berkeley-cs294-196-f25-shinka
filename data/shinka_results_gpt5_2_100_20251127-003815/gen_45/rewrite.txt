# EVOLVE-BLOCK-START
"""ARC + TinyLFU hybrid eviction policy with modular structure."""

from collections import OrderedDict

class HybridARC:
    def __init__(self):
        # Cache-resident segments
        self.T1 = OrderedDict()  # recency/probation
        self.T2 = OrderedDict()  # frequency/protected
        # Ghost segments (history of evicted keys)
        self.B1 = OrderedDict()
        self.B2 = OrderedDict()

        # ARC adaptive target (desired size of T1)
        self.p = 0.0

        # TinyLFU Count-Min Sketch
        self.SKETCH_DEPTH = 4
        self.sketch_w = 0
        self.sketch = []
        self.sketch_ops = 0
        self.age_threshold = 0

        # Misc state
        self.miss_streak = 0
        self.last_access_seen = -1

        # Track last chosen victim to place into the correct ghost
        self.last_victim_key = None
        self.last_victim_from = None  # 'T1' or 'T2' or None

    # ---------- Run lifecycle ----------
    def _reset_if_new_run(self, cache_snapshot):
        if cache_snapshot.access_count <= 1 or self.last_access_seen > cache_snapshot.access_count:
            self.T1.clear(); self.T2.clear()
            self.B1.clear(); self.B2.clear()
            self.p = 0.0
            self.sketch_w = 0
            self.sketch = []
            self.sketch_ops = 0
            self.age_threshold = 0
            self.miss_streak = 0
            self.last_victim_key = None
            self.last_victim_from = None
        self.last_access_seen = cache_snapshot.access_count

    def _prune_metadata(self, cache_snapshot):
        cache_keys = cache_snapshot.cache.keys()
        for seg in (self.T1, self.T2):
            stale = [k for k in seg.keys() if k not in cache_keys]
            for k in stale:
                seg.pop(k, None)
        # Ghosts can contain anything (history); no pruning needed beyond size bound

    def _seed_from_cache(self, cache_snapshot):
        if not self.T1 and not self.T2 and cache_snapshot.cache:
            for k in cache_snapshot.cache.keys():
                self.T1[k] = None

    # ---------- TinyLFU ----------
    def _ensure_sketch(self, cache_snapshot):
        if self.sketch_w:
            return
        cap = max(1, int(cache_snapshot.capacity))
        target = max(512, 4 * cap)  # capacity-aware width
        # next power of two
        w = 1
        while w < target:
            w <<= 1
        self.sketch_w = w
        self.sketch = [[0] * self.sketch_w for _ in range(self.SKETCH_DEPTH)]
        self.sketch_ops = 0
        # Age period within [4C, 16C] ops, start mid
        self.age_threshold = max(4 * cap, min(16 * cap, 8 * cap))

    def _hash_idx(self, key, i):
        return (hash((key, i, 0x9E3779B97F4A7C15)) & (self.sketch_w - 1))

    def _sketch_add(self, cache_snapshot, key, delta=1):
        self._ensure_sketch(cache_snapshot)
        if not self.sketch_w:
            return
        for i in range(self.SKETCH_DEPTH):
            self.sketch[i][self._hash_idx(key, i)] += delta
        self.sketch_ops += 1
        if self.sketch_ops >= self.age_threshold:
            for i in range(self.SKETCH_DEPTH):
                row = self.sketch[i]
                for j in range(self.sketch_w):
                    row[j] >>= 1
            self.sketch_ops = 0

    def _sketch_est(self, cache_snapshot, key):
        self._ensure_sketch(cache_snapshot)
        if not self.sketch_w:
            return 0
        est = None
        for i in range(self.SKETCH_DEPTH):
            v = self.sketch[i][self._hash_idx(key, i)]
            est = v if est is None or v < est else est
        return est if est is not None else 0

    # ---------- Helpers ----------
    def _cap(self, cache_snapshot):
        return max(1, int(cache_snapshot.capacity))

    def _trim_ghosts(self, cache_snapshot):
        # Bound ghost sizes to 2×capacity combined, similar to ARC
        cap = self._cap(cache_snapshot)
        max_g = 2 * cap
        # If both exceed, remove from the larger first
        while len(self.B1) + len(self.B2) > max_g:
            if len(self.B1) > len(self.B2):
                self.B1.popitem(last=False)
            else:
                self.B2.popitem(last=False)

    def _move_to_mru(self, seg, key):
        if key in seg:
            seg.move_to_end(key, last=True)
        else:
            seg[key] = None

    def _eviction_sample(self, cache_snapshot, seg, sample_k=8):
        # sample_k keys from LRU side (or fewer), choose lowest TinyLFU est
        it = iter(seg.keys())
        candid = []
        for _ in range(sample_k):
            try:
                candid.append(next(it))
            except StopIteration:
                break
        if not candid:
            # fallback to pure LRU victim
            for k in seg.keys():
                return k
            return None
        best_k = candid[0]
        best_f = self._sketch_est(cache_snapshot, best_k)
        for k in candid[1:]:
            f = self._sketch_est(cache_snapshot, k)
            if f < best_f:
                best_f = f
                best_k = k
                if best_f == 0:
                    break
        return best_k

    def _replace_segment(self, cache_snapshot, incoming_key):
        # ARC Replace rule:
        # if |T1| >= 1 and ((incoming in B2 and |T1| == p) or |T1| > p): evict from T1 else from T2
        t1 = len(self.T1)
        t2 = len(self.T2)
        cap = self._cap(cache_snapshot)
        p_int = int(round(max(0.0, min(float(cap), self.p))))
        if t1 >= 1 and ((incoming_key in self.B2 and t1 == p_int) or (t1 > p_int)):
            return 'T1'
        # If T2 empty, must evict from T1
        if t2 == 0 and t1 > 0:
            return 'T1'
        return 'T2' if t2 > 0 else 'T1'

    # ---------- Public API methods ----------
    def evict(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)
        self._seed_from_cache(cache_snapshot)

        # Decide segment using ARC Replace rule
        seg_name = self._replace_segment(cache_snapshot, obj.key)
        if seg_name == 'T1' and self.T1:
            victim = self._eviction_sample(cache_snapshot, self.T1, sample_k=8)
        elif seg_name == 'T2' and self.T2:
            # TinyLFU guard: if chosen T2 victim is much hotter than incoming, try T1 instead if possible
            cand_t2 = self._eviction_sample(cache_snapshot, self.T2, sample_k=8)
            if cand_t2 is not None:
                f_new = self._sketch_est(cache_snapshot, obj.key)
                f_t2 = self._sketch_est(cache_snapshot, cand_t2)
                if f_t2 > f_new + 1 and len(self.T1) > 0:
                    victim = self._eviction_sample(cache_snapshot, self.T1, sample_k=8)
                    seg_name = 'T1' if victim is not None else seg_name
                else:
                    victim = cand_t2
            else:
                victim = None
        else:
            victim = None

        # Fallback: any key in cache
        if victim is None:
            for k in cache_snapshot.cache.keys():
                victim = k
                seg_name = 'T1' if k in self.T1 else ('T2' if k in self.T2 else None)
                break

        # Record last-victim segment to place into ghosts after actual eviction
        self.last_victim_key = victim
        self.last_victim_from = seg_name
        return victim

    def update_after_hit(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        key = obj.key
        # Reset miss streak and learn frequency
        self.miss_streak = 0
        self._sketch_add(cache_snapshot, key, 1)

        if key in self.T2:
            # Refresh T2
            self._move_to_mru(self.T2, key)
        elif key in self.T1:
            # T1 hit -> promote to T2
            self.T1.pop(key, None)
            self._move_to_mru(self.T2, key)
        else:
            # Metadata miss but cache hit: conservatively place in T1
            self._move_to_mru(self.T1, key)

    def update_after_insert(self, cache_snapshot, obj):
        self._reset_if_new_run(cache_snapshot)
        self._prune_metadata(cache_snapshot)
        self._ensure_sketch(cache_snapshot)

        key = obj.key
        self.miss_streak += 1

        # Learn on admission
        self._sketch_add(cache_snapshot, key, 1)

        cap = self._cap(cache_snapshot)
        # ARC adaptive p tuning based on ghost refault
        if key in self.B1:
            # Favor recency -> grow T1 target
            delta = max(1, len(self.B2) // max(1, len(self.B1)))
            self.p = min(float(cap), self.p + float(delta))
            self.B1.pop(key, None)
            # Insert into T2 since this is a re-reference
            self._move_to_mru(self.T2, key)
        elif key in self.B2:
            # Favor frequency -> shrink T1 target
            delta = max(1, len(self.B1) // max(1, len(self.B2)))
            self.p = max(0.0, self.p - float(delta))
            self.B2.pop(key, None)
            self._move_to_mru(self.T2, key)
        else:
            # Fresh miss -> insert to T1
            # Early promotion for clearly hot keys
            if self._sketch_est(cache_snapshot, key) >= 4:
                self._move_to_mru(self.T2, key)
            else:
                self._move_to_mru(self.T1, key)

        # Keep T1+T2 consistent with cache contents (size managed externally)
        # No demotions here; evictions handled by evict()

        # Bound ghosts
        self._trim_ghosts(cache_snapshot)

    def update_after_evict(self, cache_snapshot, obj, evicted_obj):
        self._reset_if_new_run(cache_snapshot)
        evk = evicted_obj.key
        # Remove from resident sets
        removed_from = None
        if evk in self.T1:
            self.T1.pop(evk, None)
            removed_from = 'T1'
        elif evk in self.T2:
            self.T2.pop(evk, None)
            removed_from = 'T2'
        else:
            # If not found, trust the last recorded victim segment
            removed_from = self.last_victim_from

        # Place into corresponding ghost
        if removed_from == 'T1':
            self.B1[evk] = None
        elif removed_from == 'T2':
            self.B2[evk] = None
        # Bound ghosts
        self._trim_ghosts(cache_snapshot)
        # Clear last victim markers if they match
        if self.last_victim_key == evk:
            self.last_victim_key = None
            self.last_victim_from = None


# Singleton policy instance
_policy = HybridARC()


def evict(cache_snapshot, obj):
    return _policy.evict(cache_snapshot, obj)


def update_after_hit(cache_snapshot, obj):
    _policy.update_after_hit(cache_snapshot, obj)


def update_after_insert(cache_snapshot, obj):
    _policy.update_after_insert(cache_snapshot, obj)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    _policy.update_after_evict(cache_snapshot, obj, evicted_obj)

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