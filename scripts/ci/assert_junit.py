#!/usr/bin/env python3
"""
Assert properties of a pytest junit report, so a mandatory CI job cannot pass by running nothing or skipping.

  assert_junit.py REPORT.xml --max-skipped 0 --min-tests 300
  assert_junit.py REPORT.xml --allow-skip test_ocr_backend_selection --allow-skip test_tesseract_integration

Always fails on any failure or error. ``--max-skipped N`` bounds the skips; each ``--allow-skip SUBSTRING`` names an
optional component whose tests may skip (anything else skipped fails).
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from typing import List, Tuple


def evaluate(path: str, max_skipped=None, min_tests=None, allow_skip=()) -> Tuple[List[str], str]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    tests = sum(int(s.attrib.get("tests", 0)) for s in suites)
    failures = sum(int(s.attrib.get("failures", 0)) for s in suites)
    errors = sum(int(s.attrib.get("errors", 0)) for s in suites)
    skipped_cases = []
    for case in root.iter("testcase"):
        skip = case.find("skipped")
        if skip is not None:
            skipped_cases.append((f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}", skip.attrib.get("message", "")))
    problems: List[str] = []
    if failures or errors:
        problems.append(f"{failures} failure(s) and {errors} error(s) in the report")
    if min_tests is not None and tests < min_tests:
        problems.append(f"only {tests} tests ran, expected at least {min_tests} (a wrong selection would otherwise pass)")
    if max_skipped is not None and len(skipped_cases) > max_skipped:
        problems.append(f"{len(skipped_cases)} skipped test(s), at most {max_skipped} allowed")
    if allow_skip:
        for name, message in skipped_cases:
            if not any(token in name for token in allow_skip):
                problems.append(f"unexpected skip outside the allowed optional components: {name} ({message[:80]})")
    summary = f"{tests} tests, {failures} failures, {errors} errors, {len(skipped_cases)} skipped"
    return problems, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report")
    parser.add_argument("--max-skipped", type=int)
    parser.add_argument("--min-tests", type=int)
    parser.add_argument("--allow-skip", action="append", default=[])
    args = parser.parse_args(argv)
    problems, summary = evaluate(args.report, args.max_skipped, args.min_tests, tuple(args.allow_skip))
    print(f"junit: {summary}")
    for problem in problems:
        print(f"  FAIL: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
