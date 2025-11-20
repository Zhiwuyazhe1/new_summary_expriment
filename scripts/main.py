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


def _write_temp_saargs_file(summary_dir: str, widen_loops: bool = True) -> str:
    """Create a temporary saargs file pointing analyzer to `summary_dir`.

    Returns the absolute path to the temporary file. Caller should remove it.
    """
    summary_dir_expanded = os.path.abspath(summary_dir)
    contents = (
        "-Xanalyzer -analyzer-config\n"
        "-Xanalyzer clear-overlap-offset=true\n"
        "-Xanalyzer -analyzer-config\n"
        f"-Xanalyzer summary-dir={summary_dir_expanded}\n"
        "-Xanalyzer -analyzer-max-loop -Xanalyzer 8\n"
        "-Xanalyzer -analyzer-config\n"
        "-Xanalyzer mode=deep"
    )

    # Optionally enable widen-loops analyzer config. Default behavior keeps it enabled
    # to preserve existing behavior; callers may pass widen_loops=False to omit it.
    if widen_loops:
        contents = contents + "\n" + "-Xanalyzer -analyzer-config\n" + "-Xanalyzer widen-loops=true"

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
    # Default projects base follows the repository convention: <repo_root>/projects
    # so callers normally don't need to pass --projects-base. If you store
    # projects elsewhere you can still override with --projects-base.
    p.add_argument("--projects-base", default=os.path.join(repo_root, 'projects'), help="Base dir with project variants (default: <repo_root>/projects)")
    p.add_argument("--summaries-base", default=os.path.join(repo_root, 'summaries'), help="Base summaries dir")
    p.add_argument("--null-summary-dir", default=os.path.join(repo_root, 'null_summary'), help="Fallback null summary dir")
    p.add_argument("--reports-root", default=os.path.join(repo_root, 'reports'), help="Where to place CodeChecker reports")
    p.add_argument("--findings-root", default=os.path.join(repo_root, 'findings'), help="Where to place extractor outputs")
    p.add_argument("--intermediates-root", default=os.path.join(repo_root, 'intermediates'), help="Base dir for intermediate JSONs (groundtruth/candidates)")
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
    # Single parameter control for widen-loops: presence of the flag enables it.
    # If omitted, widen-loops is disabled.
    p.add_argument("--widen-loops", dest="widen_loops", action="store_true", default=False,
                   help="If present, enable widen-loops analyzer config; otherwise it is disabled")
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

    # determine project subdir for mode (e.g. <projects_base>/groundtruth)
    projects_base = args.projects_base
    # Special-case: when running in 'method' mode we want to use the
    # baseline projects tree (projects/baseline) as the source projects.
    # This keeps method runs using the same source projects as baseline
    # while still naming outputs under the 'method' mode.
    mode_for_projects = 'baseline' if args.mode == 'method' else args.mode
    projects_base = os.path.join(projects_base, mode_for_projects)

    # Determine list of projects to process. Support multiple config layouts:
    #  - cfg["enabled_projects"]: explicit list of projects to run (preferred)
    #  - cfg["projects"]: legacy explicit list
    #  - cfg["disabled_projects"]: legacy list of disabled projects -> enumerate dir and filter
    #  - otherwise: enumerate immediate subdirectories under projects_base
    projects = []
    if isinstance(cfg, dict):
        if 'enabled_projects' in cfg:
            projects = cfg.get('enabled_projects') or []
        elif 'projects' in cfg:
            projects = cfg.get('projects') or []
        elif 'disabled_projects' in cfg:
            disabled = set(cfg.get('disabled_projects') or [])
            if os.path.isdir(projects_base):
                try:
                    entries = sorted([d for d in os.listdir(projects_base) if os.path.isdir(os.path.join(projects_base, d))])
                    projects = [d for d in entries if d not in disabled]
                    print(f"Discovered projects in {projects_base} (filtered disabled): {projects}")
                except Exception as e:
                    print(f"Failed to enumerate projects under {projects_base}: {e}")
                    projects = []
            else:
                print(f"projects_base not found when applying disabled filter: {projects_base}")
                projects = []
        else:
            projects = []
    else:
        projects = []

    # If config.json did not define projects (and no enabled/disabled lists given),
    # enumerate immediate subdirectories under the selected projects_base/mode directory.
    if not projects:
        if os.path.isdir(projects_base):
            try:
                entries = sorted([d for d in os.listdir(projects_base) if os.path.isdir(os.path.join(projects_base, d))])
                projects = entries
                print(f"Discovered projects in {projects_base}: {projects}")
            except Exception as e:
                print(f"Failed to enumerate projects under {projects_base}: {e}")
                projects = []
        else:
            print(f"no projects specified in config.json and projects_base not found: {projects_base}")
            return 0

    if not projects:
        print("no projects to process")
        return 0

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
        # Place outputs under a subdir named by the selected mode so runs for
        # different modes don't clobber each other. e.g. reports/groundtruth/<proj>
        reports_dir = os.path.abspath(os.path.join(args.reports_root, args.mode, proj))
        # extractor should write intermediate JSON into the intermediates tree
        # so comparator can find groundtruth and candidate intermediate files
        # under intermediates/<mode>/<proj>
        findings_dir = os.path.abspath(os.path.join(args.intermediates_root, args.mode, proj))
        compare_out_base = os.path.abspath(os.path.join(args.compare_root, args.mode))

        # ensure per-project output dirs. For codechecker we want to clear
        # the reports dir before running so generated plists are fresh.
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
                build_cmd = 'make'
                output_name = 'compile_commands.json'
                # Respect global --execute: default is dry-run (do not execute external build/CodeChecker)
                if not args.execute:
                    codechecker_exe = shutil.which('CodeChecker') or shutil.which('codechecker') or 'CodeChecker'
                    make_exe = shutil.which('make')
                    print(f"DRY RUN: would run in {project_src}:")
                    # suggest running configure if detected
                    cfg_script = None
                    for cand in ('configure', 'Configure'):
                        cand_path = os.path.join(project_src, cand)
                        if os.path.isfile(cand_path):
                            cfg_script = cand
                            break
                    cmakelists = os.path.join(project_src, 'CMakeLists.txt')
                    if os.path.isfile(cmakelists):
                        print(f"  - configure: 'cmake .' (cwd={project_src})")
                    elif cfg_script:
                        print(f"  - configure: './{cfg_script}' or 'sh ./{cfg_script}' (cwd={project_src})")
                    if make_exe:
                        print(f"  - build clean: 'make clean' (cwd={project_src})")
                    print(f"  - CodeChecker record: '{codechecker_exe} log --build \"{build_cmd}\" --output {output_name}' (cwd={project_src})")
                    print(f"DRY RUN: expected compile_commands.json at {os.path.join(project_src, output_name)}")
                else:
                    # perform configure step if applicable, then generate compile_commands
                    try:
                        # compile_mod provides configure_project to run project's configure
                        compile_mod.configure_project(project_src)
                    except Exception as e:
                        print(f"configure step failed for {proj}: {e}", file=sys.stderr)
                    compile_mod.generate_compile_commands(project_src, build_cmd=build_cmd, output=output_name)
            except Exception as e:
                print(f"compile step failed for {proj}: {e}", file=sys.stderr)

            # prepare summary dir selection and temp saargs
            # Note: summaries are only used for the 'method' mode. For
            # 'groundtruth' and 'baseline' modes we intentionally do not
            # point the analyzer at any summary directory.
            summary_dir_for_proj = _resolve_summary_dir(args.summaries_base, args.summary, proj, args.null_summary_dir)
            temp_saargs_path = None
            # Only create and pass an saargs file when running CodeChecker and
            # when the selected mode is 'method' (which represents candidate runs
            # that may use summaries). groundtruth/baseline modes ignore summaries.
            if 'codechecker' in modules and args.summary != 'none' and args.mode == 'method':
                # create temp saargs file pointing to chosen summary dir
                try:
                    # args.widen_loops is a boolean flag (True if --widen-loops was provided)
                    temp_saargs_path = _write_temp_saargs_file(summary_dir_for_proj, widen_loops=bool(args.widen_loops))
                except Exception as e:
                    print(f"Failed to write temp saargs file: {e}", file=sys.stderr)
                    temp_saargs_path = None
            # If we're doing a dry-run (not actually executing CodeChecker), print
            # the contents of the temporary saargs file to help debugging.
            if temp_saargs_path and not args.execute:
                try:
                    with open(temp_saargs_path, 'r', encoding='utf-8') as _f:
                        _saargs_contents = _f.read()
                    print(f"DRY RUN: temporary saargs file at {temp_saargs_path} contents:\n{_saargs_contents}")
                except Exception as e:
                    print(f"Unable to read temp saargs file for debug output: {e}", file=sys.stderr)

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
                        # Force CodeChecker to use 32 parallel jobs; callers can still
                        # override other extra args via other interfaces if needed.
                        extra_args=["-j32"],
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
                    # pass project_src so file paths in the generated JSON
                    # are made relative to the project source tree
                    extractor_mod.generate_intermediate(reports_dir, findings_dir, project_name=proj, project_root=project_src)
                except Exception as e:
                    print(f"extractor failed for {proj}: {e}", file=sys.stderr)

            # comparator is run globally after all projects are processed so it
            # can compare the entire groundtruth set against the mode-scoped
            # candidate directory (e.g. findings/<mode>/). We skip per-project
            # comparator runs here.

        finally:
            # cleanup temporary saargs file
            try:
                if temp_saargs_path and os.path.exists(temp_saargs_path):
                    os.remove(temp_saargs_path)
            except Exception:
                pass

    # After processing all projects, run comparator once comparing the
    # ground-truth directory to the mode-scoped findings directory.
    if 'comparator' in modules:
        try:
            # Use intermediates tree for groundtruth and candidate intermediate JSONs
            gt_root = os.path.abspath(os.path.join(args.intermediates_root, 'groundtruth'))
            compare_candidates = os.path.abspath(os.path.join(args.intermediates_root, args.mode))
            os.makedirs(compare_out_base, exist_ok=True)
            print(f"Running global comparator: groundtruth={gt_root} compare={compare_candidates} outdir={compare_out_base}")
            try:
                comparator_mod.main(['--groundtruth', gt_root, '--compare', compare_candidates, '--outdir', compare_out_base])
            except SystemExit as se:
                if se.code:
                    print(f"comparator exited with code {se.code}")
        except Exception as e:
            print(f"global comparator failed: {e}", file=sys.stderr)

    print("All done")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
