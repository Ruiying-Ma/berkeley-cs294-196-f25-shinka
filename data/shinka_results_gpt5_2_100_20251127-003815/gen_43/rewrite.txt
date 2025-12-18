# EVOLVE-BLOCK-START
"""Adaptive Segmented-LRU + TinyLFU policy with modular design and online tuning."""

from collections import OrderedDict

# ----------------------------
# TinyLFU: capacity-aware sketch with conservative updates and adaptive aging
# ----------------------------
class TinyLFUSketch:
    def __init__(self):
        self.depth = 4
        self.w = 0
        self.rows = []
        self.ops = 0
        self.age_period = 0
        self.min_age = 0
        self.max_age = 0

    @staticmethod
    def _next_pow2(x: int) -> int:
        p = 1
        while p < x:
            p <<= 1
        return p

    def ensure(self, capacity: int):
        if self.w:
            return
        cap = max(1, int(capacity))
        target = max(512, 4 * cap)
        self.w = self._next_pow2(target)
        self.rows = [[0] * self.w for _ in range(self.depth)]
        # Age bounds ~ [4C, 16C]
        self.min_age = max(4 * cap, self.w // 4)
        self.max_age = max(16 * cap, self.w)
        # Start mid-way
        self.age_period = (self.min_age + self.max_age) // 2
        self.ops = 0

    def _idx(self, key, i):
        return (hash((key, i, 0x9E3779B97F4A7C15)) & (self.w - 1))

    def estimate(self, key) -> int:
        if not self.w:
            return 0
        est = None
        for i in range(self.depth):
            v = self.rows[i][self._idx(key, i)]
            est = v if est is None or v < est else est
        return est or 0

    def add(self, key, delta=1, conservative=True):
        if not self.w:
            return
        # Conservative update: increment only counters equal to current min
        idxs = [self._idx(key, i) for i in range(self.depth)]
        vals = [self.rows[i][idxs[i]] for i in range(self.depth)]
        m = min(vals)
        for i in range(self.depth):
            if not conservative or self.rows[i][idxs[i]] == m:
                self.rows[i][idxs[i]] = self.rows[i][idxs[i]] + delta
        self.ops += 1
        if self.ops >= self.age_period:
            for i in range(self.depth):
                row = self.rows[i]
                # In-place halving
                for j in range(self.w):
                    row[j] >>= 1
            self.ops = 0

    def retune_age(self, tighter: bool):
        # Move age_period toward min_age (tighter, faster forgetting) or max_age (slower)
        if tighter:
            self.age_period = max(self.min_age, self.age_period - max(1, (self.age_period - self.min_age) // 4))
        else:
            self.age_period = min(self.max_age, self.age_period + max(1, (self.max_age - self.age_period) // 4))


# ----------------------------
# Segmented LRU: probationary (W1) + protected (W2)
# ----------------------------
class SegmentedLRU:
    def __init__(self):
        self.prob = OrderedDict()     # W1: probationary
        self.prot = OrderedDict()     # W2: protected

    def prune(self, live_keys):
        live = set(live_keys)
        for seg in (self.prob, self.prot):
            dead = [k for k in seg.keys() if k not in live]
            for k in dead:
                seg.pop(k, None)

    def seed_from_cache(self, live_keys):
        if not self.prob and not self.prot:
            for k in live_keys:
                self.prob[k] = None

    def in_prob(self, k): return k in self.prob
    def in_prot(self, k): return k in self.prot
    def touch_prob(self, k): 
        if k in self.prob: self.prob.move_to_end(k, last=True)
    def touch_prot(self, k): 
        if k in self.prot: self.prot.move_to_end(k, last=True)

    def remove(self, k):
        self.prob.pop(k, None)
        self.prot.pop(k, None)

    def insert_prob_mru(self, k):
        self.remove(k)
        self.prob[k] = None

    def promote_to_prot(self, k):
        self.prob.pop(k, None)
        self.prot[k] = None

    def demote_prot_lru_to_prob(self):
        if not self.prot:
            return None
        k, _ = self.prot.popitem(last=False)
        self.prob[k] = None
        return k

    def ensure_protected_target(self, target_count: int):
        # Demote oldest until protected size <= target
        while len(self.prot) > target_count:
            self.demote_prot_lru_to_prob()

    def choose_sampled_lru(self, which: str, est_func, k: int = 8):
        seg = self.prob if which == "prob" else self.prot
        if not seg:
            return None, None
        # Iterate from LRU to MRU; pick candidate with lowest estimated frequency
        it = iter(seg.keys())
        cand = []
        for _ in range(k):
            try:
                cand.append(next(it))
            except StopIteration:
                break
        if not cand:
            return None, None
        best = cand[0]
        best_f = est_func(best)
        for c in cand[1:]:
            f = est_func(c)
            if f < best_f:
                best_f = f
                best = c
                if best_f == 0:
                    break
        return best, best_f


# ----------------------------
# Recent ring (tiny recency bias for new phases)
# ----------------------------
class RecentRing:
    def __init__(self):
        self.q = OrderedDict()
        self.limit = 0

    def ensure_limit(self, limit: int):
        self.limit = max(1, int(limit))

    def note(self, k):
        if self.limit <= 0:
            return
        if k in self.q:
            self.q.move_to_end(k, last=True)
        else:
            self.q[k] = None
        while len(self.q) > self.limit:
            self.q.popitem(last=False)

    def has(self, k) -> bool:
        return k in self.q


# ----------------------------
# Policy Orchestrator
# ----------------------------
class Policy:
    def __init__(self):
        self.inited = False
        self.last_access_seen = -1
        self.sketch = TinyLFUSketch()
        self.slru = SegmentedLRU()
        self.recent = RecentRing()

        # Adaptive control
        self.prot_frac = 0.7  # target protected fraction of capacity
        self.miss_streak = 0

        # Stats for tuning
        self.hits_prob = 0
        self.hits_prot = 0
        self.promotions = 0
        self.demotions = 0

        self.last_tune_access = 0
        self.last_hits = 0

    # -------------
    # Lifecycle
    # -------------
    def _reset(self, capacity):
        self.inited = True
        self.last_access_seen = -1
        self.sketch = TinyLFUSketch()
        self.sketch.ensure(capacity)
        self.slru = SegmentedLRU()
        self.recent = RecentRing()
        self.recent.ensure_limit(capacity)
        self.prot_frac = 0.7
        self.miss_streak = 0
        self.hits_prob = 0
        self.hits_prot = 0
        self.promotions = 0
        self.demotions = 0
        self.last_tune_access = 0
        self.last_hits = 0

    def _ensure_run(self, cache_snapshot):
        # Reset when a new run is detected
        if not self.inited or cache_snapshot.access_count <= 1 or self.last_access_seen > cache_snapshot.access_count:
            self._reset(max(1, int(cache_snapshot.capacity)))
        self.last_access_seen = cache_snapshot.access_count
        # Ensure sketch and ring are sized
        self.sketch.ensure(max(1, int(cache_snapshot.capacity)))
        self.recent.ensure_limit(max(1, int(cache_snapshot.capacity)))
        # Sync metadata with actual cache
        self.slru.prune(cache_snapshot.cache.keys())
        if not self.slru.prob and not self.slru.prot and cache_snapshot.cache:
            self.slru.seed_from_cache(cache_snapshot.cache.keys())
        # Maintain protected target
        self._enforce_protected_target(cache_snapshot)

    # -------------
    # Adaptation
    # -------------
    def _protected_target_count(self, capacity):
        cap = max(1, int(capacity))
        tgt = int(round(self.prot_frac * cap))
        if cap > 1:
            tgt = max(1, min(cap - 1, tgt))
        else:
            tgt = 1
        return tgt

    def _enforce_protected_target(self, cache_snapshot):
        target = self._protected_target_count(cache_snapshot.capacity)
        self.slru.ensure_protected_target(target)

    def _tune(self, cache_snapshot):
        # Periodically adapt prot_frac and sketch age based on segment performance and miss streak
        period = max(256, int(max(1, cache_snapshot.capacity)))
        if cache_snapshot.access_count - self.last_tune_access < period:
            return
        access_delta = cache_snapshot.access_count - self.last_tune_access
        hit_delta = cache_snapshot.hit_count - self.last_hits
        hr = (hit_delta / access_delta) if access_delta > 0 else 0.0

        # Adjust protected fraction:
        # - If probation hits dominate or long miss streak -> shrink protected (favor recency)
        # - If protected hits dominate -> grow protected (retain hot items)
        total_seg_hits = self.hits_prob + self.hits_prot
        prob_share = (self.hits_prob / total_seg_hits) if total_seg_hits > 0 else 0.5
        prot_share = 1.0 - prob_share

        if self.miss_streak > 2 * cache_snapshot.capacity or prob_share > 0.6:
            self.prot_frac = max(0.55, self.prot_frac - 0.05)
            self.sketch.retune_age(tighter=True)   # forget faster during scans/recency phases
        elif prot_share > 0.7 and hr > 0.2:
            self.prot_frac = min(0.9, self.prot_frac + 0.05)
            self.sketch.retune_age(tighter=False)  # preserve long-term during steady hot sets

        # Reset segment counters for next window
        self.hits_prob = 0
        self.hits_prot = 0
        self.promotions = 0
        self.demotions = 0
        self.last_tune_access = cache_snapshot.access_count
        self.last_hits = cache_snapshot.hit_count

    # -------------
    # Helpers
    # -------------
    def _freq_estimate(self, key):
        base = self.sketch.estimate(key)
        # Small recent bias
        if self.recent.has(key):
            base += 1
        return base

    def _choose_victim(self, cache_snapshot, new_key):
        # Two-way sampling with bias toward probation
        k1, f1 = self.slru.choose_sampled_lru("prob", self._freq_estimate, k=8)
        k2, f2 = self.slru.choose_sampled_lru("prot", self._freq_estimate, k=6)
        if k1 is None and k2 is None:
            # Fallback to any key from cache
            for k in cache_snapshot.cache.keys():
                return k
            return None
        if k1 is None:
            return k2
        if k2 is None:
            return k1

        # Bias: normally evict from probation unless clearly colder in protected
        bias = 1
        if self.miss_streak > cache_snapshot.capacity:
            bias = 2  # stronger W1 preference during scans

        f_new = self._freq_estimate(new_key)

        # If new item is hot, allow evicting from protected if its LRU is much colder
        # Otherwise, prefer probation unless protected LRU is strictly colder beyond bias.
        if f_new >= (f1 + f2) // 2:
            # Hot admission: pick the colder among the two
            return k1 if (f1 <= f2) else k2
        else:
            # Default path with bias to probation
            return k1 if (f1 <= f2 + bias) else k2

    # -------------
    # Public API (wired into framework)
    # -------------
    def on_evict(self, cache_snapshot, obj):
        self._ensure_run(cache_snapshot)
        victim = self._choose_victim(cache_snapshot, obj.key)
        return victim

    def on_hit(self, cache_snapshot, obj):
        self._ensure_run(cache_snapshot)
        k = obj.key
        self.miss_streak = 0
        # Learn frequency conservatively
        self.sketch.add(k, 1, conservative=True)
        self.recent.note(k)

        if self.slru.in_prot(k):
            self.slru.touch_prot(k)
            self.hits_prot += 1
        elif self.slru.in_prob(k):
            # Promote on probation hit
            self.slru.promote_to_prot(k)
            self.hits_prob += 1
            self.promotions += 1
        else:
            # Metadata miss but cache hit: reinsert guided by freq
            if self._freq_estimate(k) >= 2:
                self.slru.prot[k] = None
            else:
                self.slru.prob[k] = None

        # Keep protected near target via demotions
        before = len(self.slru.prot)
        self._enforce_protected_target(cache_snapshot)
        self.demotions += max(0, before - len(self.slru.prot))

        # Periodic tuning
        self._tune(cache_snapshot)

    def on_insert(self, cache_snapshot, obj):
        self._ensure_run(cache_snapshot)
        k = obj.key
        self.miss_streak += 1
        # Doorkeeper credit
        self.sketch.add(k, 1, conservative=True)
        self.recent.note(k)

        # Always place new items into probation to avoid polluting protected
        self.slru.remove(k)
        self.slru.prob[k] = None

        # Maintain protected capacity bound
        self._enforce_protected_target(cache_snapshot)
        self._tune(cache_snapshot)

    def on_post_evict(self, cache_snapshot, obj, evicted_obj):
        self._ensure_run(cache_snapshot)
        evk = evicted_obj.key
        # Remove from metadata but keep sketch memory to bias future admissions
        self.slru.remove(evk)
        # Note evicted in recent ring to help immediate re-entries avoid thrash
        self.recent.note(evk)
        # No tuning needed here


# Global singleton policy
_POL = Policy()


def evict(cache_snapshot, obj):
    """
    Choose an eviction victim using two-way sampled SLRU + TinyLFU guidance.
    """
    return _POL.on_evict(cache_snapshot, obj)


def update_after_hit(cache_snapshot, obj):
    """
    Update metadata on cache hit: conservative frequency learning and SLRU promotion.
    """
    _POL.on_hit(cache_snapshot, obj)


def update_after_insert(cache_snapshot, obj):
    """
    Update metadata on cache insert (miss path): doorkeeper credit and probation admission.
    """
    _POL.on_insert(cache_snapshot, obj)


def update_after_evict(cache_snapshot, obj, evicted_obj):
    """
    Update metadata after eviction: purge from segments (keep sketch long-term memory).
    """
    _POL.on_post_evict(cache_snapshot, obj, evicted_obj)
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