#!/usr/bin/env python3
"""
Quick evolution runner using only gpt-5-mini
"""

from shinka.core import EvolutionRunner, EvolutionConfig
from shinka.database import DatabaseConfig
from shinka.launch import LocalJobConfig

job_config = LocalJobConfig(eval_program_path="evaluate.py")

db_config = DatabaseConfig(
    db_path="evolution_db_gpt5mini.sqlite",
    num_islands=1,
    archive_size=20,
    elite_selection_ratio=0.3,
    num_archive_inspirations=2,
    num_top_k_inspirations=1,
    migration_interval=10,
    migration_rate=0.1,
    parent_selection_strategy="weighted",
    parent_selection_lambda=10.0,
)

search_task_sys_msg = """You are an expert in data optimization and LLM prompt caching. Your task is to evolve the DataFrame reordering algorithm to maximize prefix hit count (PHC) for efficient LLM prompt caching.

Problem: Reorder DataFrame columns so consecutive rows share long common prefixes, enabling efficient LLM prompt caching.

Objective:
- Maximize prefix hit rate (95% weight)
- Minimize algorithm runtime (5% weight)
- Combined score = 0.95 × hit_rate + 0.05 × runtime_score

Key Methods to Optimize:
- find_max_group_value: Select the best grouping value
- reorder_columns_for_value: Determine column order for grouped rows
- recursive_reorder: Main recursive reordering logic
- column_recursion: Handle column-wise recursion within groups

Focus on:
1. Better grouping strategies (multi-value, hierarchical, graph-based)
2. Smarter column ordering heuristics
3. Adaptive algorithms based on data characteristics
4. Efficient early stopping and pruning

Constraints:
- Preserve all data (no loss of information)
- Return DataFrame with same shape as input
- Keep memory usage reasonable for large datasets
- Maintain the required API signatures

Be creative and try novel approaches that improve both hit rate and runtime!"""

evo_config = EvolutionConfig(
    task_sys_msg=search_task_sys_msg,
    patch_types=["diff", "full"],
    patch_type_probs=[0.7, 0.3],
    num_generations=100,
    max_parallel_jobs=2,
    max_patch_resamples=3,
    max_patch_attempts=3,
    job_type="local",
    language="python",
    llm_models=["gpt-5-mini"],
    llm_kwargs=dict(
        temperatures=[0.7],
        max_tokens=16384,
    ),
    meta_rec_interval=10,
    meta_llm_models=["gpt-5-mini"],
    meta_llm_kwargs=dict(temperatures=[0.5], max_tokens=8192),
    embedding_model="text-embedding-3-small",
    code_embed_sim_threshold=0.995,
    novelty_llm_models=["gpt-5-mini"],
    novelty_llm_kwargs=dict(temperatures=[0.5], max_tokens=8192),
    init_program_path="initial.py",
    results_dir="results_llm_sql_gpt5mini",
)


def main():
    """Run the evolution process with gpt-5-mini."""
    print("=" * 60)
    print("LLM SQL Prefix Optimizer - Evolution with GPT-5-mini")
    print("=" * 60)
    print(f"Model: gpt-5-mini")
    print(f"Generations: {evo_config.num_generations}")
    print(f"Parallel jobs: {evo_config.max_parallel_jobs}")
    print(f"Results directory: {evo_config.results_dir}")
    print("=" * 60)
    print()
    
    evo_runner = EvolutionRunner(
        evo_config=evo_config,
        job_config=job_config,
        db_config=db_config,
        verbose=True,
    )
    evo_runner.run()


if __name__ == "__main__":
    main()


