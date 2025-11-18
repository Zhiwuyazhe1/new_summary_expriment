"""Orchestrator script similar to `main_example.py` but lightweight and
designed to support: selectable mode (groundtruth/baseline/method),
selectable summary type (sa, llm/taint, llm/memory), and selectable
modules to run (compile, codechecker_driver, extractor, comparator).

When running the CodeChecker step this script will create a temporary
saargs file that points the analyzer at the chosen summary dir and will
remove it afterwards (Windows-safe NamedTemporaryFile usage).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from typing import List, Optional
import time

# import local helper scripts. Support two invocation styles:
#  - python -m scripts.main  (package imports work)
#  - python scripts/main.py  (script-style; package name 'scripts' may not be importable)
try:
    # preferred when run as a package (python -m scripts.main)
    from scripts import codechecker_driver as codechecker_mod
    from scripts import extractor as extractor_mod
    from scripts import comparator as comparator_mod
    from scripts import compile as compile_mod
except Exception:
    # fallback to direct module imports when executed as a script from inside the scripts/ dir
    # This happens when sys.path[0] is the scripts/ directory and 'scripts' package is not resolvable.
    import codechecker_driver as codechecker_mod
    import extractor as extractor_mod
    import comparator as comparator_mod
    import compile as compile_mod


def _write_temp_saargs_file(summary_dir: str) -> str:
    """Create a temporary saargs file pointing analyzer to `summary_dir`.

    Returns the absolute path to the temporary file. Caller should remove it.
    """
    summary_dir_expanded = os.path.abspath(summary_dir)
    contents = (
        "-Xanalyzer -analyzer-config\n"
        "-Xanalyzer clear-overlap-offset=true\n"
        "-Xanalyzer -analyzer-config\n"
        f"-Xanalyzer summary-dir={summary_dir_expanded}\n"
        "-Xanalyzer -analyzer-max-loop -Xanalyzer 8"
    )

    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".saargs", encoding="utf-8")
    try:
        tmp.write(contents)
        tmp.flush()
        return tmp.name
    finally:
        tmp.close()


def _ensure_empty_dir(path: str) -> None:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    p = argparse.ArgumentParser(description="Main orchestrator: compile/codechecker/extract/compare")
    # prefer the `configs/config.json` layout used in this workspace, but allow
    # overriding with --config. We still accept a top-level config.json as
    # a fallback (some examples use that layout).
    p.add_argument("--config", default=os.path.join(repo_root, 'configs', 'config.json'), help="Path to config.json")
    p.add_argument("--projects-base", default=os.path.join(repo_root, 'projects'), help="Base dir with project variants")
    p.add_argument("--summaries-base", default=os.path.join(repo_root, 'summaries'), help="Base summaries dir")
    p.add_argument("--null-summary-dir", default=os.path.join(repo_root, 'null_summary'), help="Fallback null summary dir")
    p.add_argument("--reports-root", default=os.path.join(repo_root, 'reports'), help="Where to place CodeChecker reports")
    p.add_argument("--findings-root", default=os.path.join(repo_root, 'findings'), help="Where to place extractor outputs")
    p.add_argument("--compare-root", default=os.path.join(repo_root, 'results'), help="Comparator outputs")
    p.add_argument("--ground-truth-base", default=os.path.join(repo_root, 'groundtruth'), help="Groundtruth base dir")

    p.add_argument("--mode", choices=["groundtruth", "baseline", "method"], default="baseline",
                   help="Select which projects variant to use (maps to a subdir under --projects-base)")

    p.add_argument("--summary", choices=["sa", "llm/taint", "llm/memory", "none"], default="sa",
                   help="Which summary source to use for saargs (none -> use null_summary)")

    p.add_argument("--modules", default="all",
                   help="Comma-separated list of modules to run: compile,codechecker,extractor,comparator or 'all'")

    p.add_argument("--execute", action="store_true", help="Actually run CodeChecker instead of dry-run")
    p.add_argument("--codechecker-bin", default="CodeChecker", help="CodeChecker executable path")
    p.add_argument("--no-ctu", action="store_true", help="Disable CTU in CodeChecker run")
    p.add_argument("--timeout", type=int, default=None, help="Timeout seconds for CodeChecker run")
    p.add_argument("--verbose", action="store_true")

    return p.parse_args(argv)


def _resolve_summary_dir(summaries_base: str, selection: str, proj: str, null_summary_dir: str) -> str:
    if selection == 'none':
        return null_summary_dir
    if selection == 'sa':
        # summaries/sa/<proj>
        return os.path.join(summaries_base, 'sa', proj)
    if selection == 'llm/taint':
        return os.path.join(summaries_base, 'llm', 'taint', proj)
    if selection == 'llm/memory':
        return os.path.join(summaries_base, 'llm', 'memory', proj)
    return null_summary_dir


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    # load config.json (support common locations with a small fallback)
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    cfg_path = os.path.abspath(os.path.expanduser(args.config))
    if not os.path.exists(cfg_path):
        # try workspace convention: configs/config.json, then top-level config.json
        alt1 = os.path.join(repo_root, 'configs', 'config.json')
        alt2 = os.path.join(repo_root, 'config.json')
        chosen = None
        for alt in (alt1, alt2):
            if os.path.exists(alt):
                chosen = alt
                break
        if chosen:
            print(f"config not found at {cfg_path}; using {chosen}")
            cfg_path = chosen
        else:
            print(f"config.json not found: {cfg_path}")
            return 2

    with open(cfg_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    projects = cfg.get('projects', []) if isinstance(cfg, dict) else []
    if not projects:
        print("no projects specified in config.json")
        return 0

    # determine project subdir for mode
    projects_base = args.projects_base
    projects_base = os.path.join(projects_base, args.mode)

    # parse modules
    if args.modules.strip().lower() == 'all':
        modules = {'compile', 'codechecker', 'extractor', 'comparator'}
    else:
        modules = set([m.strip().lower() for m in args.modules.split(',') if m.strip()])

    os.makedirs(args.reports_root, exist_ok=True)
    os.makedirs(args.findings_root, exist_ok=True)
    os.makedirs(args.compare_root, exist_ok=True)

    for proj in projects:
        print(f"=== processing project: {proj} ===")
        project_src = os.path.abspath(os.path.join(projects_base, proj))
        reports_dir = os.path.abspath(os.path.join(args.reports_root, proj))
        findings_dir = os.path.abspath(os.path.join(args.findings_root, proj))
        compare_out_base = os.path.abspath(args.compare_root)

        # ensure dirs
        if 'codechecker' in modules:
            _ensure_empty_dir(reports_dir)
        else:
            os.makedirs(reports_dir, exist_ok=True)

        if 'extractor' in modules:
            _ensure_empty_dir(findings_dir)
        else:
            os.makedirs(findings_dir, exist_ok=True)

        # optionally run compile step
        compile_commands_path = os.path.join(project_src, 'compile_commands.json')
        if 'compile' in modules:
            try:
                print(f"Running compile step for {proj} (produces compile_commands.json)")
                # Use default build command and output name
                compile_mod.generate_compile_commands(project_src, build_cmd='make', output='compile_commands.json')
            except Exception as e:
                print(f"compile step failed for {proj}: {e}", file=sys.stderr)

        # prepare summary dir selection and temp saargs
        summary_dir_for_proj = _resolve_summary_dir(args.summaries_base, args.summary, proj, args.null_summary_dir)
        temp_saargs_path = None
        if 'codechecker' in modules and args.summary != 'none':
            # create temp saargs file pointing to chosen summary dir
            try:
                temp_saargs_path = _write_temp_saargs_file(summary_dir_for_proj)
            except Exception as e:
                print(f"Failed to write temp saargs file: {e}", file=sys.stderr)
                temp_saargs_path = None

        try:
            # run codechecker
            if 'codechecker' in modules:
                print(f"Calling CodeChecker for {proj} -> reports at {reports_dir} (dry_run={not args.execute})")
                try:
                    codechecker_mod.run_codechecker(
                        saargs=temp_saargs_path,
                        output_dir=reports_dir,
                        compile_commands=compile_commands_path,
                        ctu=not args.no_ctu,
                        codechecker_bin=args.codechecker_bin,
                        extra_args=None,
                        cwd=project_src if os.path.isdir(project_src) else None,
                        timeout=args.timeout,
                        dry_run=not args.execute,
                        verbose=args.verbose,
                    )
                except Exception as e:
                    print(f"CodeChecker run failed for {proj}: {e}", file=sys.stderr)

            # extractor
            if 'extractor' in modules:
                try:
                    print(f"Extracting reports from {reports_dir} -> {findings_dir}")
                    # call generate_intermediate which returns path to json
                    extractor_mod.generate_intermediate(reports_dir, findings_dir, project_name=proj)
                except Exception as e:
                    print(f"extractor failed for {proj}: {e}", file=sys.stderr)

            # comparator
            if 'comparator' in modules:
                try:
                    # comparator expects groundtruth and candidate dirs/files
                    gt_dir = os.path.abspath(os.path.join(args.ground_truth_base, proj))
                    print(f"Running comparator: groundtruth={gt_dir} compare={findings_dir} outdir={compare_out_base}")
                    comparator_mod.main(['--groundtruth', gt_dir, '--compare', findings_dir, '--outdir', compare_out_base])
                except SystemExit as se:
                    # comparator may call sys.exit; capture code
                    if se.code:
                        print(f"comparator exited with code {se.code}")
                except Exception as e:
                    print(f"comparator failed for {proj}: {e}", file=sys.stderr)

        finally:
            # cleanup temporary saargs file
            try:
                if temp_saargs_path and os.path.exists(temp_saargs_path):
                    os.remove(temp_saargs_path)
            except Exception:
                pass

    print("All done")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
