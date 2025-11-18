"""Run CodeChecker analysis (wrapper).

This script builds and runs a CodeChecker `analyze` command configured to use
the clang static analyzer (clangsa). It's intended to be called from
`main_script.py` or used standalone from the command line.

Example command constructed by this script:
  CodeChecker analyze --analyzers clangsa \
	--saargs "<clangsa_cfg_string>" --ctu \
	--capture-analysis-output compile_commands.json \
	-o <reports_dir>

The implementation exposes a `run_codechecker` function that returns the
subprocess.CompletedProcess result and raises subprocess.CalledProcessError on
non-zero exit (unless called with check=False).
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
import json
from typing import List, Optional



def run_codechecker(
	saargs: Optional[str],
	output_dir: str,
	compile_commands: str,
	ctu: bool = True,
	codechecker_bin: str = "CodeChecker",
	extra_args: Optional[List[str]] = None,
	cwd: Optional[str] = None,
	timeout: Optional[int] = None,
	dry_run: bool = False,
	verbose: bool = False,
	check: bool = False,
) -> subprocess.CompletedProcess:
	"""Build and run the CodeChecker analyze command.

	Args:
		saargs: The clang static analyzer config string (passed to --saargs).
		output_dir: Directory where CodeChecker will write the report outputs.
		compile_commands: Path for the compile_commands.json capture output.
		ctu: Whether to enable CTU (cross translation unit) analysis.
		codechecker_bin: Executable name or path for CodeChecker.
		extra_args: Additional command-line tokens to append.
		cwd: Working directory to run the command in.
		timeout: Timeout in seconds for the subprocess.
		dry_run: If True, only print the command instead of executing it.
		verbose: If True, print stdout/stderr on completion.
		check: If True, raise CalledProcessError on non-zero exit.

	Returns:
		subprocess.CompletedProcess from subprocess.run

	Raises:
		subprocess.CalledProcessError: if check is True and process exits non-zero.
	"""

	if extra_args is None:
		extra_args = []

	# Ensure output directory exists
	os.makedirs(output_dir, exist_ok=True)

	cmd: List[str] = [codechecker_bin, "analyze", "--analyzers", "clangsa"]

	# saargs may be None (no extra analyzer args) or a path/string accepted by
	# CodeChecker's --saargs option. If provided, pass it as a single token.
	if saargs:
		cmd.extend(["--saargs", saargs])

	if ctu:
		cmd.append("--ctu")

	cmd.extend(["--capture-analysis-output", compile_commands])

	cmd.extend(["-o", output_dir])

	if extra_args:
		cmd.extend(extra_args)

	# For logging and dry run, build a shell-friendly string for display
	cmd_display = " ".join(shlex.quote(c) for c in cmd)

	if dry_run:
		print("DRY RUN: would execute:\n", cmd_display)
		# Return a dummy CompletedProcess
		return subprocess.CompletedProcess(cmd, 0)

	# Execute without auto-raising so callers can continue when analysis had
	# partial failures (non-zero exit). If caller requests check=True we will
	# raise after capturing stdout/stderr.
	start_ts = time.time()
	proc = subprocess.run(
		cmd,
		cwd=cwd,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		timeout=timeout,
		check=False,
	)
	elapsed = time.time() - start_ts

	# If the caller explicitly asked for check=True, raise with the captured
	# output so existing callers depending on exceptions still work.
	if proc.returncode != 0 and check:
		raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)

	if verbose:
		if proc.stdout:
			print("[CodeChecker stdout]")
			print(proc.stdout)
		if proc.stderr:
			print("[CodeChecker stderr]")
			print(proc.stderr, file=sys.stderr)

	# Persist stdout/stderr into the output directory so callers can inspect
	# the CodeChecker logs even when they don't enable --verbose.
	try:
		os.makedirs(output_dir, exist_ok=True)
		with open(os.path.join(output_dir, 'codechecker_stdout.txt'), 'w', encoding='utf-8') as sf:
			sf.write(proc.stdout or '')
		with open(os.path.join(output_dir, 'codechecker_stderr.txt'), 'w', encoding='utf-8') as ef:
			ef.write(proc.stderr or '')
		# Also save an aggregated debug file with command, return code and a
		# combined view of stdout/stderr for quick inspection.
		try:
			debug_path = os.path.join(output_dir, 'analysis_debug.txt')
			with open(debug_path, 'w', encoding='utf-8') as df:
				df.write(f"Command: {cmd_display}\n")
				df.write(f"Return code: {proc.returncode}\n")
				df.write(f"Elapsed seconds: {elapsed}\n")
				df.write("\n--- STDOUT ---\n")
				df.write(proc.stdout or '')
				df.write("\n\n--- STDERR ---\n")
				df.write(proc.stderr or '')
		except Exception:
			pass

		# If saargs refers to a file (e.g. a temporary .saargs created by
		# the orchestrator), copy its contents into the output dir so it's
		# available for debugging.
		try:
			if saargs and os.path.isfile(saargs):
				with open(saargs, 'r', encoding='utf-8') as sf_in:
					sa_contents = sf_in.read()
				with open(os.path.join(output_dir, 'saargs_used.saargs'), 'w', encoding='utf-8') as sf_out:
					sf_out.write(sa_contents)
		except Exception:
			pass
		# Persist a small analysis_time.json so downstream tools (extractor)
		# can read timing metadata even when this helper is used directly.
		try:
			metadata = {
				"start_timestamp": start_ts,
				"end_timestamp": start_ts + elapsed,
				"elapsed_seconds": elapsed,
				"saargs_dir": saargs,
				"compile_commands": compile_commands,
				"codechecker_bin": codechecker_bin,
				"ctu_enabled": ctu,
				"extra_args": extra_args,
			}
			meta_path = os.path.join(output_dir, "analysis_time.json")
			with open(meta_path, 'w', encoding='utf-8') as mf:
				json.dump(metadata, mf, indent=2)
		except Exception:
			# Best-effort: failures to write metadata should not affect analysis
			pass
	except Exception:
		# Ignore failures to write logs; do not change analysis behavior
		pass

	return proc


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Run CodeChecker analyze for clangsa")

	# Path to compile_commands.json (produced by scripts/compile.py) and
	# output directory where CodeChecker will write analysis reports.
	# Use --compile-commands to pass the path to the compile_commands.json file
	# and --output-dir to specify the reports directory.
	p.add_argument(
		"--compile-commands",
		required=True,
		help="Path to compile_commands.json (generated by scripts/compile.py)",
	)

	p.add_argument(
		"--output-dir",
		required=True,
		help="Directory where CodeChecker will write the analysis reports",
	)

	p.add_argument(
		"--saargs-dir",
		default=None,
		help="Directory containing summaries to use when --use-summary is set",
	)

	# Note: CodeChecker analyzer configuration is provided via --saargs-dir
	# (saargs_dir). We intentionally do not add a separate --codechecker-config
	# argument to avoid duplication; saargs_dir will be recorded in metadata.

	p.add_argument("--no-ctu", action="store_true", help="Disable CTU analysis")
	p.add_argument(
		"--codechecker-bin",
		default="CodeChecker",
		help="CodeChecker executable name/path",
	)
	p.add_argument("--dry-run", action="store_true", help="Print the command but do not run it")
	p.add_argument("--verbose", action="store_true", help="Print stdout/stderr from CodeChecker")
	p.add_argument("--cwd", default=None, help="Working directory to run CodeChecker in")
	p.add_argument("--timeout", type=int, default=None, help="Timeout seconds for CodeChecker run")
	p.add_argument("--extra-args", nargs=argparse.REMAINDER, help="Extra args appended to the CodeChecker command")

	return p.parse_args(argv)



def main(argv: Optional[List[str]] = None) -> int:
	args = parse_args(argv)

	# Record start time and ensure output dir exists (run_codechecker will
	# also ensure it exists but we want to be able to write metadata in case
	# the run fails early).
	start_ts = time.time()
	os.makedirs(args.output_dir, exist_ok=True)

	return_code = 0
	try:
		proc = run_codechecker(
			saargs=args.saargs_dir,
			output_dir=args.output_dir,
			compile_commands=args.compile_commands,
			ctu=not args.no_ctu,
			codechecker_bin=args.codechecker_bin,
			extra_args=args.extra_args,
			cwd=args.cwd,
			timeout=args.timeout,
			dry_run=args.dry_run,
			verbose=args.verbose,
		)
		# If CodeChecker ran without raising, capture its return code.
		return_code = getattr(proc, "returncode", 0)
	except subprocess.CalledProcessError as exc:
		# Preserve existing behavior: print stdout/stderr then return the
		# non-zero exit code, but still record timing metadata.
		print("CodeChecker failed with return code:", exc.returncode, file=sys.stderr)
		if exc.stdout:
			print(exc.stdout)
		if exc.stderr:
			print(exc.stderr, file=sys.stderr)
		return_code = exc.returncode
	finally:
		end_ts = time.time()
		elapsed = end_ts - start_ts

		metadata = {
			"start_timestamp": start_ts,
			"end_timestamp": end_ts,
			"elapsed_seconds": elapsed,
			"saargs_dir": args.saargs_dir,
			"compile_commands": args.compile_commands,
			"codechecker_bin": args.codechecker_bin,
			"ctu_enabled": not args.no_ctu,
			"extra_args": args.extra_args,
		}

		try:
			meta_path = os.path.join(args.output_dir, "analysis_time.json")
			with open(meta_path, "w", encoding="utf-8") as mf:
				json.dump(metadata, mf, indent=2)
			if not args.dry_run:
				print(f"Wrote analysis metadata to {meta_path}")
		except Exception as e:
			print("Failed to write analysis metadata:", e, file=sys.stderr)

	return return_code


if __name__ == "__main__":
	raise SystemExit(main())


