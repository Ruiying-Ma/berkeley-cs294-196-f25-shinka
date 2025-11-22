#!/usr/bin/env python3
"""
Local evolution runner for LLM SQL Prefix Optimizer
"""

from shinka.core import EvolutionRunner, EvolutionConfig
from shinka.database import DatabaseConfig
from shinka.launch import LocalJobConfig

# Configure local job execution
job_config = LocalJobConfig(eval_program_path="evaluate.py")

# Parent selection strategy
strategy = "weighted"
if strategy == "uniform":
    # 1. Uniform from correct programs
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=0.0,
        exploitation_ratio=1.0,
    )
elif strategy == "hill_climbing":
    # 2. Hill Climbing (Always from the Best)
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=100.0,
        exploitation_ratio=1.0,
    )
elif strategy == "weighted":
    # 3. Weighted Prioritization
    parent_config = dict(
        parent_selection_strategy="weighted",
        parent_selection_lambda=10.0,
    )
elif strategy == "power_law":
    # 4. Power-Law Prioritization
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=1.0,
        exploitation_ratio=0.2,
    )
elif strategy == "power_law_high":
    # 5. Power-Law Prioritization (High Alpha)
    parent_config = dict(
        parent_selection_strategy="power_law",
        exploitation_alpha=2.0,
        exploitation_ratio=0.2,
    )
elif strategy == "beam_search":
    # 6. Beam Search
    parent_config = dict(
        parent_selection_strategy="beam_search",
        num_beams=10,
    )

# Database configuration
db_config = DatabaseConfig(
    db_path="evolution_db.sqlite",
    num_islands=2,
    archive_size=40,
    # Inspiration parameters
    elite_selection_ratio=0.3,
    num_archive_inspirations=4,
    num_top_k_inspirations=2,
    # Island migration parameters
    migration_interval=10,
    migration_rate=0.1,  # Chance to migrate program to random island
    island_elitism=True,  # Island elite is protected from migration
    **parent_config,
)

# Task system message for LLM evolution
search_task_sys_msg = """You are an expert in data optimization and LLM prompt caching. Your task is to evolve the DataFrame reordering algorithm to maximize prefix hit count (PHC) for efficient LLM prompt caching.

Problem Context:
- You are given a pandas DataFrame with text data in rows and columns
- The goal is to reorder columns (and optionally rows) to maximize prefix reuse when processing rows sequentially
- Prefix reuse occurs when consecutive rows have matching character sequences starting from the beginning
- This reduces LLM computation costs by reusing cached prefixes

Objective:
- Dual objective: (1) maximize prefix hit rate across consecutive rows and (2) minimize algorithm runtime
- Your goal is to evolve the Evolved class such that when rows are processed sequentially, they reuse as much prefix as possible
- Prefix reuse is measured by counting matching characters from the start of each row compared to all previously seen rows
- The combined score balances accuracy (95% weight) and speed (5% weight)

Formally:
- For a given column ordering C, PHC(C) = sum over all rows r of longest_common_prefix(r, all_previous_rows)
- Hit rate = PHC / total_string_length (normalized to 0-1)
- Runtime is measured in wall-clock seconds
- Combined score = 0.95 * average_hit_rate + 0.05 * (12 - min(12, average_runtime)) / 12

Required API (DO NOT CHANGE):
- Keep the Evolved class structure and reorder method signature
- The reorder method must accept: df, early_stop, row_stop, col_stop, col_merge, one_way_dep, distinct_value_threshold, parallel
- Must return: Tuple[pd.DataFrame, List[List[str]]]

Algorithm Design Guidelines:
1. Group rows by common values to maximize shared prefixes
2. Order columns by their contribution to prefix hits (frequency × length²)
3. Use recursive grouping to handle hierarchical patterns
4. Consider parallel processing for scalability
5. Implement early stopping for efficiency
6. Handle high-cardinality columns appropriately
7. Support column merging for related fields
8. Optimize the balance between greedy local decisions and global optimality

Key Methods to Optimize:
- find_max_group_value: Select the best grouping value
- reorder_columns_for_value: Determine column order for grouped rows
- recursive_reorder: Main recursive reordering logic
- column_recursion: Handle column-wise recursion within groups
- recursive_split_and_reorder: Parallel divide-and-conquer strategy

Constraints:
- Preserve all rows and data (no loss of information)
- Return DataFrame with same shape as input (except for column merges)
- Keep memory usage reasonable for large datasets
- Maintain deterministic behavior for reproducibility

Be creative and explore novel approaches:
- Alternative grouping strategies (multi-value, hierarchical, graph-based)
- Better heuristics for column ordering
- Adaptive algorithms that learn from data characteristics
- Hybrid approaches combining multiple strategies
- Machine learning-inspired optimization techniques

The algorithm will be evaluated on 5 diverse datasets:
1. Movies database (entertainment data)
2. Beer reviews (product data)
3. BIRD (forum posts)
4. PDMX (metadata)
5. Products (e-commerce data)

Focus on generalizable improvements that work well across different data distributions."""

# Evolution configuration
evo_config = EvolutionConfig(
    task_sys_msg=search_task_sys_msg,
    patch_types=["diff", "full", "cross"],
    patch_type_probs=[0.6, 0.3, 0.1],
    num_generations=400,
    max_parallel_jobs=3,
    max_patch_resamples=3,
    max_patch_attempts=3,
    job_type="local",
    language="python",
    llm_models=[
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "bedrock/us.anthropic.claude-sonnet-4-20250514-v1:0",
        "o4-mini",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
    ],
    llm_kwargs=dict(
        temperatures=[0.0, 0.5, 1.0],
        reasoning_efforts=["auto", "low", "medium", "high"],
        max_tokens=32768,
    ),
    meta_rec_interval=10,
    meta_llm_models=["gpt-5-nano"],
    meta_llm_kwargs=dict(temperatures=[0.0], max_tokens=16384),
    embedding_model="text-embedding-3-small",
    code_embed_sim_threshold=0.995,
    novelty_llm_models=["gpt-5-nano"],
    novelty_llm_kwargs=dict(temperatures=[0.0], max_tokens=16384),
    llm_dynamic_selection="ucb1",
    llm_dynamic_selection_kwargs=dict(exploration_coef=1.0),
    init_program_path="initial.py",
    results_dir="results_llm_sql",
)


def main():
    """Run the evolution process."""
    evo_runner = EvolutionRunner(
        evo_config=evo_config,
        job_config=job_config,
        db_config=db_config,
        verbose=True,
    )
    evo_runner.run()


if __name__ == "__main__":
    results_data = main()


