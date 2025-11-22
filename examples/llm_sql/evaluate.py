"""
Evaluator for LLM SQL Prefix Optimizer example
"""

import os
import argparse
import sys
import pandas as pd
from typing import Tuple, Optional, List, Dict, Any

from shinka.core import run_shinka_eval

# Add the llm_sql directory to the path to import utilities
def find_llm_sql_dir(start_path):
    """Find the llm_sql directory by searching upward from current location."""
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):
        if os.path.basename(current) == 'llm_sql' and os.path.exists(os.path.join(current, 'utils.py')):
            return current
        llm_sql_path = os.path.join(current, 'examples', 'llm_sql')
        if os.path.exists(os.path.join(llm_sql_path, 'utils.py')):
            return llm_sql_path
        current = os.path.dirname(current)
    raise RuntimeError("Could not find llm_sql directory with utils.py")

llm_sql_dir = find_llm_sql_dir(os.path.dirname(__file__))
sys.path.insert(0, llm_sql_dir)

from utils import evaluate_df_prefix_hit_cnt

# Dataset configurations with absolute paths
DATASET_DIR = os.path.join(llm_sql_dir, 'datasets')

TEST_FILES = [
    os.path.join(DATASET_DIR, "movies.csv"),
    os.path.join(DATASET_DIR, "beer.csv"),
    os.path.join(DATASET_DIR, "BIRD.csv"),
    os.path.join(DATASET_DIR, "PDMX.csv"),
    os.path.join(DATASET_DIR, "products.csv"),
]

COL_MERGES = [
    [['movieinfo', 'movietitle', 'rottentomatoeslink']],
    [['beer/beerId', 'beer/name']],
    [['PostId', 'Body']],
    [['path', 'metadata'], ['hasmetadata', 'isofficial', 'isuserpublisher', 'isdraft', 'hasannotations', 'subsetall']],
    [['product_title', 'parent_asin']],
]


def format_results_string(hit_rates: List[float], total_runtime: float) -> str:
    """Formats LLM SQL results into a multi-line string for display."""
    result = []
    for i, hit_rate in enumerate(hit_rates):
        result.append(f"  dataset_{i+1}_hit_rate = {hit_rate:.4f}")
    result.append(f"  total_runtime = {total_runtime:.4f}s")
    return "\n".join(result)


def adapted_validate_llm_sql(
    run_output: Tuple[pd.DataFrame, float],
    atol=1e-6,
) -> Tuple[bool, Optional[str]]:
    """
    Validates LLM SQL optimizer results based on the output of 'run_llm_sql_optimizer'.

    Args:
        run_output: Tuple (reordered_dataframe, execution_time) from run_llm_sql_optimizer.

    Returns:
        (is_valid: bool, error_message: Optional[str])
    """
    reordered_df, execution_time = run_output
    msg = "The reordered DataFrame is valid."
    
    # Check that output is a DataFrame
    if not isinstance(reordered_df, pd.DataFrame):
        msg = f"Expected pd.DataFrame, got {type(reordered_df)}"
        return False, msg
    
    # Check that DataFrame is not empty
    if reordered_df.empty:
        msg = "Reordered DataFrame is empty"
        return False, msg
    
    # Check execution time is reasonable
    if execution_time < 0:
        msg = f"Execution time should be non-negative, got {execution_time}"
        return False, msg
    
    # Check for excessive NaN values (allow some NaN, but not if entire DataFrame is NaN)
    if reordered_df.isnull().all().all():
        msg = "Reordered DataFrame is entirely NaN"
        return False, msg
    
    return True, msg


def get_llm_sql_kwargs(run_index: int) -> Dict[str, Any]:
    """
    Provides keyword arguments for LLM SQL optimizer runs.
    
    Each run uses a different dataset with corresponding column merge configuration.
    """
    if run_index >= len(TEST_FILES):
        # Fallback for testing
        return {
            "df": pd.DataFrame({"A": [1, 2, 3], "B": [4, 5, 6]}),
            "early_stop": 100000,
            "distinct_value_threshold": 0.7,
            "row_stop": 4,
            "col_stop": 2,
            "col_merge": [],
            "parallel": False,
        }
    
    dataset_path = TEST_FILES[run_index]
    col_merge = COL_MERGES[run_index]
    
    # Check if dataset exists
    if not os.path.exists(dataset_path):
        raise FileNotFoundError(f"Dataset not found: {dataset_path}")
    
    # Load the dataset
    df = pd.read_csv(dataset_path)
    
    return {
        "df": df,
        "early_stop": 100000,
        "distinct_value_threshold": 0.7,
        "row_stop": 4,
        "col_stop": 2,
        "col_merge": col_merge,
        "parallel": False,  # Disable parallel processing for pickling compatibility
    }


def aggregate_llm_sql_metrics(
    results: List[Tuple[pd.DataFrame, float]], 
    results_dir: str
) -> Dict[str, Any]:
    """
    Aggregates metrics for LLM SQL optimizer across multiple dataset runs.
    """
    if not results:
        return {"combined_score": 0.0, "error": "No results to aggregate"}

    hit_rates = []
    runtimes = []
    total_prefix_hit_counts = []
    
    for reordered_df, runtime in results:
        try:
            # Evaluate the reordered DataFrame
            prefix_hit_count, hit_rate = evaluate_df_prefix_hit_cnt(reordered_df)
            hit_rates.append(hit_rate / 100)  # Normalize to 0-1
            runtimes.append(runtime)
            total_prefix_hit_counts.append(prefix_hit_count)
        except Exception as e:
            print(f"Error evaluating result: {str(e)}")
            return {
                "combined_score": 0.0,
                "error": f"Evaluation failed: {str(e)}",
            }
    
    if not hit_rates:
        return {"combined_score": 0.0, "error": "No valid results to aggregate"}
    
    # Calculate aggregate metrics
    average_hit_rate = sum(hit_rates) / len(hit_rates)
    total_runtime = sum(runtimes)
    average_runtime = total_runtime / len(runtimes)
    
    # Combined score: 95% hit rate + 5% runtime penalty
    # Runtime penalty: capped at 12 seconds, normalized to 0-1
    runtime_score = (12 - min(12, average_runtime)) / 12
    combined_score = 0.95 * average_hit_rate + 0.05 * runtime_score
    
    public_metrics = {
        "results_str": format_results_string(hit_rates, total_runtime),
        "num_datasets_evaluated": len(hit_rates),
    }
    private_metrics = {
        "average_hit_rate": float(average_hit_rate),
        "total_runtime": float(total_runtime),
        "average_runtime": float(average_runtime),
        "runtime_score": float(runtime_score),
        "hit_rates": [float(hr) for hr in hit_rates],
        "runtimes": [float(rt) for rt in runtimes],
        "total_prefix_hit_counts": [int(phc) for phc in total_prefix_hit_counts],
    }
    
    metrics = {
        "combined_score": float(combined_score),
        "public": public_metrics,
        "private": private_metrics,
    }

    return metrics


def main(program_path: str, results_dir: str):
    """Runs the LLM SQL evaluation using shinka.eval."""
    print(f"Evaluating program: {program_path}")
    print(f"Saving results to: {results_dir}")
    os.makedirs(results_dir, exist_ok=True)

    # Verify datasets exist
    missing_datasets = [f for f in TEST_FILES if not os.path.exists(f)]
    if missing_datasets:
        print(f"Warning: Missing datasets: {missing_datasets}")
        print(f"Please ensure datasets are in: {DATASET_DIR}")
    
    # Number of datasets to evaluate
    num_experiment_runs = len([f for f in TEST_FILES if os.path.exists(f)])
    
    if num_experiment_runs == 0:
        print("Error: No datasets found for evaluation")
        return

    # Define a nested function to pass results_dir to the aggregator
    def _aggregator_with_context(
        r: List[Tuple[pd.DataFrame, float]],
    ) -> Dict[str, Any]:
        return aggregate_llm_sql_metrics(r, results_dir)

    metrics, correct, error_msg = run_shinka_eval(
        program_path=program_path,
        results_dir=results_dir,
        experiment_fn_name="run_llm_sql_optimizer",
        num_runs=num_experiment_runs,
        get_experiment_kwargs=get_llm_sql_kwargs,
        validate_fn=adapted_validate_llm_sql,
        aggregate_metrics_fn=_aggregator_with_context,
    )

    if correct:
        print("Evaluation and Validation completed successfully.")
    else:
        print(f"Evaluation or Validation failed: {error_msg}")

    print("Metrics:")
    for key, value in metrics.items():
        if isinstance(value, str) and len(value) > 100:
            print(f"  {key}: <string_too_long_to_display>")
        else:
            print(f"  {key}: {value}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="LLM SQL optimizer evaluator using shinka.eval"
    )
    parser.add_argument(
        "--program_path",
        type=str,
        default="initial.py",
        help="Path to program to evaluate (must contain 'run_llm_sql_optimizer')",
    )
    parser.add_argument(
        "--results_dir",
        type=str,
        default="results",
        help="Dir to save results (metrics.json, correct.json)",
    )
    parsed_args = parser.parse_args()
    main(parsed_args.program_path, parsed_args.results_dir)

