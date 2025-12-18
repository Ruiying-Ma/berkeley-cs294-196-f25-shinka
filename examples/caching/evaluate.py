"""
Evaluator for Caching example
"""
import os
import argparse
import numpy as np
from typing import Tuple, Optional, List, Dict, Any

from shinka.core import run_shinka_eval

TRACE_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'trace')
TRACE_FILES = [
    os.path.join(TRACE_FOLDER, f)
    for f in sorted(os.listdir(TRACE_FOLDER))
]

def format_results_string(hit_rates: List[float]) -> str:
    """Formats Caching results into a multi-line string for display."""
    result = []
    for i, hit_rate in enumerate(hit_rates):
        result.append(f"  trace_{i+1}_hit_rate = {hit_rate:.4f}")
    return "\n".join(result)

def adapted_validate_caching(
    run_output: float,
    atol=1e-6,
) -> Tuple[bool, Optional[str]]:
    """
    Validates Caching results based on the output of 'run_caching'.

    Args:
        run_output: float (hit_rate) from run_caching.

    Returns:
        (is_valid: bool, error_message: Optional[str])
    """
    # Check that output is a hit rate
    if not isinstance(run_output, float) or not (0.0 <= run_output <= 1.0):
        msg = f"Expected hit rate as float between 0 and 1, got {run_output}"
        return False, msg
    
    msg = "The hit rate is valid."
    
    return True, msg


def get_caching_kwargs(run_index: int) -> Dict[str, Any]:
    """
    Provides keyword arguments for Caching runs.
    
    Each run uses a different trace.
    """

    assert 0 <= run_index < len(TRACE_FILES), f"Invalid run index: {run_index}"
    
    trace_path = TRACE_FILES[run_index]
    copy_code_dst_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "My.py"
    )
    
    # Check if trace exists
    if not os.path.exists(trace_path):
        raise FileNotFoundError(f"Trace not found: {trace_path}")
    # Check if copy_code_dst_path exists
    if not os.path.exists(copy_code_dst_path):
        raise FileNotFoundError(f"Copy code destination not found: {copy_code_dst_path}")
    
    return {
        "trace_path": trace_path,
        "copy_code_dst": copy_code_dst_path,
    }


def aggregate_caching_metrics(
    results: List[float], 
    results_dir: str
) -> Dict[str, Any]:
    """
    Aggregates metrics for Caching across multiple trace runs.
    """
    if not results:
        return {"combined_score": 0.0, "error": "No results to aggregate"}
    
    if len(results) == 0:
        return {"combined_score": 0.0, "error": "No results to aggregate"}
    
    if len([r for r in results if (isinstance(r, float) and (0 <= r <= 1))]) != len(TRACE_FILES):
        return {
            "combined_score": 0.0,
            "error": f"Expected {len(TRACE_FILES)} valid results, got {len(results)}",
        }
    

    # Calculate aggregate metrics
    average_hit_rate = sum(results) / len(results)
    
    public_metrics = {
        "results_str": format_results_string(results),
        "num_traces_evaluated": len(TRACE_FILES),
    }
    private_metrics = {
        "average_hit_rate": float(average_hit_rate),
        "hit_rates": [float(hr) for hr in results],
    }
    
    metrics = {
        "combined_score": average_hit_rate,
        "public": public_metrics,
        "private": private_metrics,
    }

    return metrics


def main(program_path: str, results_dir: str):
    """Runs the Caching evaluation using shinka.eval."""
    print(f"Evaluating program: {program_path}")
    print(f"Saving results to: {results_dir}")
    os.makedirs(results_dir, exist_ok=True)

    # Number of datasets to evaluate
    num_experiment_runs = len(TRACE_FILES)
    
    # Define a nested function to pass results_dir to the aggregator
    def _aggregator_with_context(
        r: List[float],
    ) -> Dict[str, Any]:
        return aggregate_caching_metrics(r, results_dir)

    metrics, correct, error_msg = run_shinka_eval(
        program_path=program_path,
        results_dir=results_dir,
        experiment_fn_name="run_caching",
        num_runs=num_experiment_runs,
        get_experiment_kwargs=get_caching_kwargs,
        validate_fn=adapted_validate_caching,
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
        description="Caching evaluator using shinka.eval"
    )
    parser.add_argument(
        "--program_path",
        type=str,
        default="initial.py",
        help="Path to program to evaluate (must contain 'run_caching')",
    )
    parser.add_argument(
        "--results_dir",
        type=str,
        default="results",
        help="Dir to save results (metrics.json, correct.json)",
    )
    parsed_args = parser.parse_args()
    main(parsed_args.program_path, parsed_args.results_dir)