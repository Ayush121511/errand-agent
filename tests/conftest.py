"""Write docs/eval_results.md from the scenario results when the full suite ran."""


def pytest_sessionfinish(session, exitstatus):
    from evals.scenarios import REGISTRY
    from tests.test_scenarios import RESULTS

    if RESULTS and len(RESULTS) == len(REGISTRY):
        from evals.run import write_report

        write_report(RESULTS)
