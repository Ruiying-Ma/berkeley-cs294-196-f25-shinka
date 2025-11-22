# EVOLVE-BLOCK-START
"""
LLM SQL Prefix Optimizer - DataFrame Column Reordering Algorithm

This module implements an algorithm to optimize DataFrame column ordering
to maximize prefix hit count for efficient LLM prompt caching.
"""

import pandas as pd
from typing import Tuple, List, Dict, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
import networkx as nx
import sys
import os
import threading
import math

# Add the llm_sql directory to the path to import required modules
def find_llm_sql_dir(start_path):
    """Find the llm_sql directory by searching upward from current location."""
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):  # Stop at filesystem root
        # Check if we're in the llm_sql directory or can find it
        if os.path.basename(current) == 'llm_sql' and os.path.exists(os.path.join(current, 'solver.py')):
            return current
        # Check if llm_sql is a subdirectory
        llm_sql_path = os.path.join(current, 'examples', 'llm_sql')
        if os.path.exists(os.path.join(llm_sql_path, 'solver.py')):
            return llm_sql_path
        current = os.path.dirname(current)
    raise RuntimeError("Could not find llm_sql directory with solver.py")

llm_sql_dir = find_llm_sql_dir(os.path.dirname(__file__))
sys.path.insert(0, llm_sql_dir)

from solver import Algorithm


class Evolved(Algorithm):
    """
    Greedy Hierarchical Reordering optimized for PHC with deterministic behavior.
    """

    def __init__(self, df: pd.DataFrame = None):
        self.df = df
        self.dep_graph: Optional[nx.DiGraph] = None
        self.num_rows = 0
        self.num_cols = 0
        self.column_stats = None
        self.val_len = None
        self.row_stop = None
        self.col_stop = None
        self.base = 2000

        # Instance caches and lock (thread-safe)
        self._dep_cache: Dict[str, List[str]] = {}
        self._len_cache: Dict[object, int] = {}
        self._cache_lock = threading.RLock()

        # Top-K groups for parallel recursion (adaptive)
        self.k_split = 3

        # Soft threshold for fallback when recursion stalls
        self.soft_score_threshold = 2.0

    # ---------------------
    # Utility / Scoring
    # ---------------------
    def calculate_length(self, value) -> int:
        """Thread-safe per-instance cached squared-length calculation."""
        with self._cache_lock:
            if value in self._len_cache:
                return self._len_cache[value]
        try:
            if value is None:
                l = 0
            else:
                l = len(str(value)) ** 2
        except Exception:
            l = 0
        with self._cache_lock:
            self._len_cache[value] = l
        return l

    def compute_column_scores(self, df: pd.DataFrame) -> Dict[str, float]:
        """
        Heuristic column scores: sum_over_values(count^2 * len(value)^2)
        This favors columns with repeated long values (high prefix reuse potential).
        Deterministic ordering if equal scores via column name.
        """
        scores = {}
        n = len(df)
        for col in df.columns:
            # compute counts
            counts = df[col].value_counts(dropna=False)
            s = 0.0
            for val, cnt in counts.items():
                if cnt <= 1:
                    continue
                l = self.calculate_length(val)
                s += (cnt ** 2) * l
            scores[col] = float(s)
        return scores

    def compute_value_score_map(self, df: pd.DataFrame) -> Dict[object, float]:
        """
        Score each cell value by how useful it is for grouping:
        score = total_occurrences * length(value) * distinct_row_count
        """
        stacked = list(df.stack())
        if not stacked:
            return {}
        total_counts = Counter(stacked)
        # distinct rows containing each value
        row_counts = Counter()
        for row in df.itertuples(index=False):
            row_counts.update(set(row))
        val_scores = {}
        for val, occ in total_counts.items():
            if occ <= 1:
                continue
            l = self.calculate_length(val)
            val_scores[val] = occ * l * max(1, row_counts.get(val, 1))
        return val_scores

    # ---------------------
    # Dependency helpers
    # ---------------------
    def get_dependent_columns(self, col: str) -> List[str]:
        """Get columns that depend on the given column (descendants in dep_graph)."""
        if self.dep_graph is None or not self.dep_graph.has_node(col):
            return []
        return sorted(nx.descendants(self.dep_graph, col))

    def get_cached_dependent_columns(self, col: str) -> List[str]:
        """Thread-safe per-instance cached version of get_dependent_columns."""
        with self._cache_lock:
            if col in self._dep_cache:
                return self._dep_cache[col]
        deps = self.get_dependent_columns(col)
        with self._cache_lock:
            self._dep_cache[col] = deps
        return deps

    # ---------------------
    # Grouping & Ordering heuristics
    # ---------------------
    def find_max_group_value(self, df: pd.DataFrame, value_scores: Dict, early_stop: float = 0.0) -> Optional[object]:
        """
        Choose best grouping value using value_scores (precomputed).
        Deterministic tie break by stringified value.
        """
        if not value_scores:
            return None
        # Filter out very low scores by early_stop
        filtered = {v: s for v, s in value_scores.items() if s >= early_stop}
        if not filtered:
            return None
        # pick max (deterministic)
        best = max(sorted(filtered.items(), key=lambda x: (x[1], str(x[0]))), key=lambda x: x[1])[0]
        return best

    def order_columns_for_group(self, df_group: pd.DataFrame, group_val: object, global_col_scores: Dict[str, float]) -> List[str]:
        """
        Given a group (rows that contain group_val somewhere), decide a column order:
        - Put columns that contain the group_val first, ordered by column score and dependency,
        - Followed by remaining columns ordered by descending column score.
        """
        cols = df_group.columns.tolist()
        cols_with_val = []
        for col in cols:
            # Consider NaNs: using equality check with care
            try:
                mask = df_group[col].isin([group_val])
            except Exception:
                # fallback when unhashable
                mask = df_group[col].apply(lambda x: x == group_val)
            if mask.any():
                cols_with_val.append(col)
        # Order cols_with_val by (global_col_scores, col name) desc
        cols_with_val = sorted(cols_with_val, key=lambda c: (global_col_scores.get(c, 0.0), c), reverse=True)
        # expand with dependencies (stable)
        expanded = []
        seen = set()
        for c in cols_with_val:
            if c in seen:
                continue
            expanded.append(c)
            seen.add(c)
            deps = self.get_cached_dependent_columns(c)
            for d in deps:
                if d in cols and d not in seen:
                    expanded.append(d)
                    seen.add(d)
        remaining = [c for c in cols if c not in seen]
        remaining = sorted(remaining, key=lambda c: (global_col_scores.get(c, 0.0), c), reverse=True)
        return expanded + remaining

    # ---------------------
    # Fixed/hybrid fallback
    # ---------------------
    def fixed_reorder(self, df: pd.DataFrame, row_sort: bool = True) -> Tuple[pd.DataFrame, List[List[str]]]:
        """
        Deterministic fixed reorder using calculate_col_stats from Algorithm if available.
        If calculate_col_stats isn't present, fallback to score-based ordering.
        """
        try:
            num_rows, column_stats = self.calculate_col_stats(df, enable_index=True)
            reordered_columns = [col for col, _, _, _ in column_stats]
        except Exception:
            # fallback using computed scores
            col_scores = self.compute_column_scores(df)
            reordered_columns = sorted(df.columns.tolist(), key=lambda c: (col_scores.get(c, 0.0), c), reverse=True)
        reordered_df = df[reordered_columns]
        assert reordered_df.shape == df.shape
        column_orderings = [reordered_columns] * len(df)
        if row_sort and len(reordered_columns) > 0:
            try:
                reordered_df = reordered_df.sort_values(by=reordered_columns, axis=0)
            except Exception:
                pass
        return reordered_df, column_orderings

    # ---------------------
    # Core recursive algorithm
    # ---------------------
    def column_recursion(self, grouped_rows: pd.DataFrame, group_val: object, global_col_scores: Dict[str, float],
                         row_stop: int, col_stop: int, early_stop: float) -> Tuple[pd.DataFrame, Counter]:
        """
        Reorder columns for the block of grouped_rows which contains group_val somewhere.
        - Decide columns order for the group.
        - Reorder each row into that order (deterministically).
        - Recurse on remainder columns if useful.
        """
        if grouped_rows.empty:
            return pd.DataFrame(columns=grouped_rows.columns), Counter()

        # Order columns for this group
        col_order = self.order_columns_for_group(grouped_rows, group_val, global_col_scores)
        # Build reordered block
        reordered_block = grouped_rows[col_order].copy()
        # Count grouped values for subtraction upstream
        grouped_value_counts = Counter(reordered_block.stack())

        # Determine settled columns (those that contain group_val in all rows? or first ones)
        settled_cols = []
        for c in col_order:
            try:
                if reordered_block[c].isin([group_val]).all():
                    settled_cols.append(c)
                else:
                    # stop at first column that isn't uniform group_val
                    if settled_cols:
                        break
            except Exception:
                # conservative
                break

        # If there are remainder columns after settled prefix, recurse on them within rows that had group_val in first settled column
        if settled_cols:
            first_settled = settled_cols[0]
            # rows where first_settled equals group_val
            mask = reordered_block[first_settled].isin([group_val])
            if mask.any():
                remainder_cols = [c for c in col_order if c not in settled_cols]
                if remainder_cols:
                    remainder = reordered_block.loc[mask, remainder_cols]
                    # compute value counts for remainder
                    remainder_counts = Counter(remainder.stack())
                    # recursive reorder of remainder columns (increase col_stop)
                    reordered_remainder, _ = self.recursive_reorder(
                        remainder,
                        remainder_counts,
                        early_stop=early_stop,
                        row_stop=row_stop,
                        col_stop=col_stop + 1,
                    )
                    # assign back deterministically using index alignment
                    try:
                        # reorder_remainder may reset index; align by original index
                        reordered_remainder.index = remainder.index
                        reordered_block.loc[mask, remainder_cols] = reordered_remainder[remainder_cols].values
                    except Exception:
                        # best-effort by values
                        for i, c in enumerate(remainder_cols):
                            reordered_block.loc[mask, c] = reordered_remainder.iloc[:, i].values if i < reordered_remainder.shape[1] else remainder[c].values
        return reordered_block, grouped_value_counts

    def recursive_reorder(
        self,
        df: pd.DataFrame,
        value_counts: Counter,
        early_stop: float = 0.0,
        original_columns: List[str] = None,
        row_stop: int = 0,
        col_stop: int = 0,
    ) -> Tuple[pd.DataFrame, List[List[str]]]:
        """
        Recursively reorder DataFrame columns and rows to maximize prefix hits.
        Hybrid approach: greedy grouping by high-scoring values with fallback to fixed_reorder.
        """
        if df.empty or len(df.columns) == 0 or len(df) == 0:
            return df, []

        if original_columns is None:
            original_columns = df.columns.tolist()

        if self.row_stop is not None and row_stop >= self.row_stop:
            return self.fixed_reorder(df)
        if self.col_stop is not None and col_stop >= self.col_stop:
            return self.fixed_reorder(df)

        # compute value scores for this frame
        val_scores = self.compute_value_score_map(df)
        best_val = self.find_max_group_value(df, val_scores, early_stop=early_stop)
        if best_val is None:
            # fallback to fixed reorder
            return self.fixed_reorder(df)

        # split rows that contain best_val and others
        mask = df.isin([best_val]).any(axis=1)
        grouped_rows = df.loc[mask].copy()
        remaining_rows = df.loc[~mask].copy()

        if grouped_rows.empty:
            return self.fixed_reorder(df)

        # compute global col scores used for ordering decisions
        global_col_scores = self.compute_column_scores(df)

        # Reorder the grouped block's columns and possibly recurse inside
        grouped_block, grouped_value_counts = self.column_recursion(grouped_rows, best_val, global_col_scores, row_stop, col_stop, early_stop)

        # Recurse on remaining rows
        remaining_value_counts = value_counts - grouped_value_counts if isinstance(value_counts, Counter) else Counter(remaining_rows.stack())
        reordered_remaining, _ = self.recursive_reorder(remaining_rows, remaining_value_counts, early_stop=early_stop, row_stop=row_stop + 1, col_stop=col_stop)

        # Concatenate grouped block first, then remaining, deterministic
        # preserve columns union and order: use grouped_block.columns then remaining columns missing padded
        # Ensure we preserve same columns set as df
        try:
            # ensure same columns
            ordered_cols = list(grouped_block.columns)
            # if some columns are missing in grouped_block (possible), append remaining columns deterministically
            for c in df.columns:
                if c not in ordered_cols:
                    ordered_cols.append(c)
            # align both blocks to ordered_cols
            gb = grouped_block.reindex(columns=ordered_cols)
            rr = reordered_remaining.reindex(columns=ordered_cols)
            final_df = pd.concat([gb, rr], axis=0, ignore_index=True)
        except Exception:
            # last resort, stack raw rows
            final_df = pd.DataFrame(list(grouped_block.values) + list(reordered_remaining.values))
            if final_df.shape[1] == df.shape[1]:
                final_df.columns = df.columns

        # If recursion made no effective grouping (i.e., single column ordering change is trivial), fallback
        # measure a tiny heuristic: if best value score is too small, fallback
        best_score = val_scores.get(best_val, 0.0)
        if best_score < self.soft_score_threshold:
            return self.fixed_reorder(df)

        return final_df, []

    # ---------------------
    # Parallel split-and-recurse (top-K groups)
    # ---------------------
    def recursive_split_and_reorder(self, df: pd.DataFrame, original_columns: List[str] = None, early_stop: float = 0.0):
        """
        Top-K value-based parallel recursion:
        - Select top-K frequent/high-scoring values
        - Partition rows into groups for each value and a remainder
        - Recurse groups in parallel using threads (safe for in-memory DataFrame)
        - Reassemble deterministically (sorted by string(key))
        """
        if len(df) <= self.base:
            initial_value_counts = Counter(df.stack())
            return self.recursive_reorder(df, initial_value_counts, early_stop, original_columns, row_stop=0, col_stop=0)[0]

        # compute value scores and choose top-k
        val_scores = self.compute_value_score_map(df)
        if not val_scores:
            # fallback to simple split
            mid = len(df) // 2
            top = df.iloc[:mid]
            bot = df.iloc[mid:]
            return pd.concat([self.recursive_split_and_reorder(top, original_columns, early_stop),
                              self.recursive_split_and_reorder(bot, original_columns, early_stop)],
                             axis=0, ignore_index=True)

        k = max(1, min(len(val_scores), getattr(self, "k_split", 3)))
        top_vals = [v for v, _ in sorted(val_scores.items(), key=lambda x: (x[1], str(x[0])), reverse=True)][:k]

        groups = []
        used_idx = set()
        for v in top_vals:
            m = df.isin([v]).any(axis=1)
            g = df.loc[m]
            if not g.empty:
                groups.append((v, g.copy()))
                used_idx.update(g.index.tolist())

        remainder_idx = sorted(set(df.index.tolist()) - used_idx)
        remainder = df.loc[remainder_idx].copy() if remainder_idx else pd.DataFrame(columns=df.columns)

        # parallel recurse groups (threads)
        workers = max(1, len(groups) + (1 if not remainder.empty else 0))
        results = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            fut_map = {}
            for val, gdf in groups:
                fut = executor.submit(self.recursive_split_and_reorder, gdf, original_columns, early_stop)
                fut_map[fut] = val
            if not remainder.empty:
                fut = executor.submit(self.recursive_split_and_reorder, remainder, original_columns, early_stop)
                fut_map[fut] = "__REMAINDER__"

            for fut in as_completed(list(fut_map.keys())):
                key = fut_map[fut]
                try:
                    res = fut.result()
                except Exception:
                    # fallback to original group (deterministic)
                    if key == "__REMAINDER__":
                        res = remainder
                    else:
                        res = next((g for v, g in groups if v == key), pd.DataFrame(columns=df.columns))
                results[key] = res

        # deterministic assembly: groups sorted by string(key)
        ordered_frames = []
        for v, _ in sorted(groups, key=lambda x: str(x[0])):
            if v in results:
                ordered_frames.append(results[v].reset_index(drop=True))
        if "__REMAINDER__" in results:
            ordered_frames.append(results["__REMAINDER__"].reset_index(drop=True))

        if not ordered_frames:
            return pd.DataFrame(columns=df.columns)

        reordered = pd.concat(ordered_frames, axis=0, ignore_index=True)

        # safety: shapes must match
        if reordered.shape[0] != df.shape[0] or set(reordered.columns) != set(df.columns):
            # fallback to deterministic two-way split
            mid = len(df) // 2
            top = df.iloc[:mid]
            bot = df.iloc[mid:]
            top_r = self.recursive_split_and_reorder(top, original_columns, early_stop)
            bot_r = self.recursive_split_and_reorder(bot, original_columns, early_stop)
            reordered = pd.concat([top_r, bot_r], axis=0, ignore_index=True)

        assert reordered.shape == df.shape
        return reordered

    # ---------------------
    # Main API
    # ---------------------
    def reorder(
        self,
        df: pd.DataFrame,
        early_stop: int = 0,
        row_stop: int = None,
        col_stop: int = None,
        col_merge: List[List[str]] = [],
        one_way_dep: List[tuple] = [],
        distinct_value_threshold: float = 0.8,
        parallel: bool = True,
    ) -> Tuple[pd.DataFrame, List[List[str]]]:
        """
        Main entry point for DataFrame reordering optimization.

        Returns:
            (reordered_df, column_orderings)
        """
        initial_df = df.copy()
        df = df.copy()

        # Merge columns if specified (use base merging helper, if available)
        if col_merge:
            try:
                self.num_rows, self.column_stats = self.calculate_col_stats(df, enable_index=True)
                reordered_columns = [col for col, _, _, _ in self.column_stats]
                for col_to_merge in col_merge:
                    final_col_order = [col for col in reordered_columns if col in col_to_merge]
                    df = self.merging_columns(df, final_col_order, prepended=False)
            except Exception:
                # best-effort: skip merging if helpers not available
                pass

        # compute column stats if available
        try:
            self.num_rows, self.column_stats = self.calculate_col_stats(df, enable_index=True)
            # transform into dict for quick access if needed
            try:
                self.column_stats = {col: (num_groups, avg_len, score) for col, num_groups, avg_len, score in self.column_stats}
            except Exception:
                pass
        except Exception:
            self.column_stats = None

        # Build one-way dependency graph if specified
        if one_way_dep:
            self.dep_graph = nx.DiGraph()
            for dep in one_way_dep:
                col1 = [c for c in df.columns if dep[0] in c]
                col2 = [c for c in df.columns if dep[1] in c]
                if len(col1) == 1 and len(col2) == 1:
                    self.dep_graph.add_edge(col1[0], col2[0])
            # clear dep cache
            with self._cache_lock:
                self._dep_cache.clear()

        # Adaptive high-cardinality pruning:
        n = len(df)
        # compute per-column unique ratio and potential gain
        col_unique_ratio = {c: (df[c].nunique(dropna=False) / max(1, n)) for c in df.columns}
        col_potential = self.compute_column_scores(df)
        # Depth-aware threshold: allow more pruning at root, less deeper
        depth = 0
        if row_stop is None:
            row_stop = len(df)
        if col_stop is None:
            col_stop = len(df.columns)
        depth_aware_threshold = distinct_value_threshold  # base threshold
        # choose columns to discard: unique ratio above threshold and low potential
        columns_to_discard = []
        for c in df.columns:
            if col_unique_ratio.get(c, 1.0) > depth_aware_threshold and col_potential.get(c, 0.0) < (max(col_potential.values()) * 0.25 if col_potential else 0):
                columns_to_discard.append(c)
        # keep deterministic order
        columns_to_discard = sorted(columns_to_discard)

        columns_to_recurse = [c for c in df.columns if c not in columns_to_discard]

        # attach original index for deterministic reassembly
        df = df.reset_index(drop=True)
        df["original_index"] = range(len(df))

        discarded_columns_df = df[columns_to_discard + ["original_index"]] if columns_to_discard else pd.DataFrame(columns=["original_index"])
        recurse_df = df[columns_to_recurse + ["original_index"]]

        # init caches for val lengths
        initial_value_counts = Counter(recurse_df.stack()) if not recurse_df.empty else Counter()
        with self._cache_lock:
            self._len_cache.clear()
        self.val_len = {val: self.calculate_length(val) for val in initial_value_counts.keys()}

        # set stop parameters
        self.row_stop = row_stop
        self.col_stop = col_stop

        # baseline fixed reorder
        recurse_df, _ = self.fixed_reorder(recurse_df)

        # apply recursive reordering
        self.num_cols = len(recurse_df.columns)
        if parallel:
            reordered_df = self.recursive_split_and_reorder(recurse_df, original_columns=columns_to_recurse, early_stop=early_stop)
        else:
            reordered_df, _ = self.recursive_reorder(recurse_df, initial_value_counts, early_stop=early_stop)

        # ensure original_index preserved and unique
        assert "original_index" in reordered_df.columns
        assert reordered_df["original_index"].is_unique

        # Merge back discarded columns deterministically by original_index
        if columns_to_discard:
            final_df = pd.merge(reordered_df, discarded_columns_df, on="original_index", how="left", sort=False)
        else:
            final_df = reordered_df

        # drop helper column and restore original columns order preference:
        try:
            final_df = final_df.drop(columns=["original_index"])
        except Exception:
            if "original_index" in final_df.columns:
                final_df = final_df.loc[:, [c for c in final_df.columns if c != "original_index"]]

        # ensure shape compatibility
        if not col_merge:
            try:
                assert final_df.shape == initial_df.shape
            except AssertionError:
                # attempt to coerce columns to initial_df columns deterministically
                # append missing columns at end (stable)
                final_cols = list(final_df.columns)
                for c in initial_df.columns:
                    if c not in final_cols:
                        final_cols.append(c)
                final_df = final_df.reindex(columns=final_cols).iloc[:, :initial_df.shape[1]]
        else:
            assert final_df.shape[0] == initial_df.shape[0]

        # final deterministic sort by all columns to create canonical order of rows
        try:
            final_df = final_df.sort_values(by=final_df.columns.to_list(), axis=0, ignore_index=True)
        except Exception:
            # if sort fails, just reset index deterministically
            final_df = final_df.reset_index(drop=True)

        return final_df, []


# EVOLVE-BLOCK-END


# This part remains fixed (not evolved)
def run_llm_sql_optimizer(
    df: pd.DataFrame,
    early_stop: int = 100000,
    distinct_value_threshold: float = 0.7,
    row_stop: int = 4,
    col_stop: int = 2,
    col_merge: List[List[str]] = [],
    parallel: bool = False,
) -> Tuple[pd.DataFrame, float]:
    """
    Run the LLM SQL prefix optimizer on a DataFrame.

    Args:
        df: Input DataFrame
        early_stop: Early stopping threshold
        distinct_value_threshold: Threshold for high-cardinality columns
        row_stop: Maximum row recursion depth
        col_stop: Maximum column recursion depth
        col_merge: Column groups to merge
        parallel: Whether to use parallel processing (default False for pickling compatibility)

    Returns:
        Tuple of (reordered_dataframe, execution_time)
    """
    import time
    start_time = time.time()

    reordered_df, _ = Evolved().reorder(
        df,
        early_stop=early_stop,
        distinct_value_threshold=distinct_value_threshold,
        row_stop=row_stop,
        col_stop=col_stop,
        col_merge=col_merge,
        parallel=parallel,
    )

    execution_time = time.time() - start_time
    return reordered_df, execution_time


__all__ = ["Evolved", "run_llm_sql_optimizer"]