"""
Shinka evaluator for network telemetry repair example.

Integrates the OpenEvolve evaluator with Shinka's evaluation framework.
"""

import os
import sys
import argparse
from typing import Dict, Any, List

from shinka.core import run_shinka_eval


# Add openevolve_examples to path to access the evaluator
def find_repo_root(start_path):
    """Find the repository root by looking for openevolve_examples directory."""
    current = os.path.abspath(start_path)
    while current != os.path.dirname(current):
        if os.path.exists(os.path.join(current, 'openevolve_examples', 'telemetry_repair')):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("Could not find openevolve_examples/telemetry_repair directory")


# Add evaluator path
repo_root = find_repo_root(os.path.dirname(__file__))
evaluator_path = os.path.join(repo_root, 'openevolve_examples', 'telemetry_repair')
if evaluator_path not in sys.path:
    sys.path.insert(0, evaluator_path)


def get_telemetry_repair_kwargs(run_index: int) -> Dict[str, Any]:
    """
    Provides keyword arguments for telemetry repair runs (none needed).
    
    Args:
        run_index: Index of the current run
        
    Returns:
        Empty dictionary (no additional kwargs needed)
    """
    return {}


def aggregate_telemetry_repair_metrics(results: List[Dict[str, float]], results_dir: str) -> Dict[str, Any]:
    """
    Aggregates metrics for telemetry repair. Assumes num_runs=1.
    
    Args:
        results: List of result dictionaries from evaluations
        results_dir: Directory to save additional results
        
    Returns:
        Dictionary with aggregated metrics
    """
    if not results:
        return {"combined_score": 0.0, "error": "No results to aggregate"}
    
    # Since we do a single comprehensive run, just return the first result
    result = results[0]
    
    # Extract the main metrics
    combined_score = result.get('combined_score', 0.0)
    counter_repair_accuracy = result.get('counter_repair_accuracy', 0.0)
    status_repair_accuracy = result.get('status_repair_accuracy', 0.0)
    confidence_calibration = result.get('confidence_calibration', 0.0)
    
    # Format public metrics (visible to LLM)
    public_metrics = {
        "counter_repair_accuracy": f"{counter_repair_accuracy:.3f}",
        "status_repair_accuracy": f"{status_repair_accuracy:.3f}",
        "confidence_calibration": f"{confidence_calibration:.3f}",
        "num_scenarios": result.get('num_scenarios', 0),
    }
    
    # Private metrics (not visible to LLM, used for analysis)
    private_metrics = {
        "detailed_results": result,
    }
    
    metrics = {
        "combined_score": float(combined_score),
        "public": public_metrics,
        "private": private_metrics,
    }
    
    return metrics


def adapted_validate_telemetry_repair(run_output: Dict[str, Any], atol=1e-6) -> tuple[bool, str | None]:
    """
    Validates telemetry repair results.
    
    Args:
        run_output: Dictionary of evaluation metrics
        atol: Absolute tolerance for comparisons
        
    Returns:
        Tuple of (is_valid: bool, error_message: Optional[str])
    """
    # Check that we got some valid results
    if not isinstance(run_output, dict):
        return False, f"Expected dict output, got {type(run_output)}"
    
    if 'combined_score' not in run_output:
        return False, "Missing combined_score in output"
    
    combined_score = run_output.get('combined_score', 0.0)
    
    # As long as we got a valid score, consider it valid
    if not isinstance(combined_score, (int, float)):
        return False, f"combined_score should be numeric, got {type(combined_score)}"
    
    return True, "Telemetry repair evaluation completed successfully"


def main(program_path: str, results_dir: str):
    """
    Main evaluation entry point called by Shinka.
    
    Directly calls the OpenEvolve evaluator instead of using run_shinka_eval
    since the evaluation logic is already comprehensive.
    
    Args:
        program_path: Path to the program file to evaluate
        results_dir: Directory to save results
    """
    print(f"Evaluating program: {program_path}")
    print(f"Saving results to: {results_dir}")
    os.makedirs(results_dir, exist_ok=True)
    
    # Import the evaluator from openevolve_examples
    try:
        import evaluator as oe_evaluator
        
        # Run the comprehensive evaluation
        result = oe_evaluator.evaluate(program_path)
        
        # Format metrics for Shinka
        combined_score = result.get('combined_score', 0.0)
        
        # Create public metrics (visible to LLM)
        public_metrics = {
            "counter_repair_accuracy": f"{result.get('counter_repair_accuracy', 0.0):.3f}",
            "status_repair_accuracy": f"{result.get('status_repair_accuracy', 0.0):.3f}",
            "confidence_calibration": f"{result.get('confidence_calibration', 0.0):.3f}",
            "num_scenarios": result.get('num_scenarios', 0),
        }
        
        # Save metrics
        import json
        metrics = {
            "combined_score": float(combined_score),
            "public": public_metrics,
            "private": result,
        }
        
        with open(os.path.join(results_dir, "metrics.json"), "w") as f:
            json.dump(metrics, f, indent=2)
        
        # Save correctness status
        is_correct = combined_score > 0.0
        with open(os.path.join(results_dir, "correct.json"), "w") as f:
            json.dump({"correct": is_correct, "error_msg": None if is_correct else "Score is 0.0"}, f, indent=2)
        
        print("✓ Evaluation completed successfully")
        print(f"\nMetrics:")
        print(f"  combined_score: {combined_score:.3f}")
        for key, value in public_metrics.items():
            print(f"  {key}: {value}")
        
        return metrics, is_correct, None
        
    except Exception as e:
        print(f"Evaluation failed: {e}")
        import traceback
        traceback.print_exc()
        
        # Save error state
        import json
        metrics = {
            'combined_score': 0.0,
            'error': str(e)
        }
        
        with open(os.path.join(results_dir, "metrics.json"), "w") as f:
            json.dump(metrics, f, indent=2)
        
        with open(os.path.join(results_dir, "correct.json"), "w") as f:
            json.dump({"correct": False, "error_msg": str(e)}, f, indent=2)
        
        return metrics, False, str(e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Telemetry repair evaluator using Shinka framework"
    )
    parser.add_argument(
        "--program_path",
        type=str,
        default="initial.py",
        help="Path to program to evaluate (must contain 'run_repair')",
    )
    parser.add_argument(
        "--results_dir",
        type=str,
        default="results",
        help="Directory to save results (metrics.json, correct.json, etc.)",
    )
    parsed_args = parser.parse_args()
    main(parsed_args.program_path, parsed_args.results_dir)

