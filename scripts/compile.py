# codechecker分析前置构建脚本 - 核心目的：生成 compile_commands.json 文件

from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional
from contextlib import contextmanager
import os


@contextmanager
def pushd(path: Path):
    old = Path.cwd()
    os.chdir(str(path))
    try:
        yield
    finally:
        os.chdir(str(old))


def configure_project(project_path: str, configure_cmd: Optional[str] = None) -> None:
    """
    Run the project's configure step if applicable.

    - If `configure_cmd` is provided, it will be executed (shell).
    - Otherwise, this function will try common defaults:
      * If a `configure` script exists in the project root, run it with `sh`.
      * If a `CMakeLists.txt` exists, run `cmake .` to configure an in-source build.

    Raises subprocess.CalledProcessError on failure.
    """
    p = Path(project_path)
    if not p.exists():
        raise FileNotFoundError(f"project_path not found: {project_path}")

    # use explicit command if provided
    if configure_cmd:
        print(f"Running configure command: {configure_cmd} (cwd={p})")
        with pushd(p):
            subprocess.run(configure_cmd, shell=True, check=True)
        return

    # detect configure script (case-insensitive, e.g., 'configure' or 'Configure')
    cmakelists = p / "CMakeLists.txt"
    configure_candidate = None
    for child in p.iterdir():
        if child.is_file() and child.name.lower() == "configure":
            configure_candidate = child
            break

    if configure_candidate is not None:
        # Decide how to run the configure script:
        #  - If the script is executable, prefer running it directly (./Configure)
        #  - If it looks like a Perl script (shebang mentioning perl or contains typical perl tokens), run with `perl`.
        #  - Otherwise fall back to `sh` (for classic configure shell scripts).
        try:
            with configure_candidate.open("r", encoding="utf-8", errors="ignore") as f:
                first_line = f.readline()
                # read a bit more to detect tokens like 'use strict' that indicate Perl
                rest = f.read(4096)
        except Exception:
            first_line = ""
            rest = ""

        cmd = None
        # prefer executing directly if the file is marked executable
        if os.access(str(configure_candidate), os.X_OK):
            cmd = [f"./{configure_candidate.name}"]
        # if shebang mentions perl or file contains typical perl tokens, use perl
        elif ("perl" in first_line.lower()) or ("use strict" in rest) or rest.lstrip().startswith("use "):
            cmd = ["perl", configure_candidate.name]
        else:
            # fallback to sh for Bourne shell style configure scripts
            cmd = ["sh", configure_candidate.name]

        print(f"Found configure script ({configure_candidate.name}), running: {' '.join(cmd)} (cwd={p})")
        with pushd(p):
            subprocess.run(cmd, check=True)
        return

    if cmakelists.exists():
        # run cmake to configure in-source (users can change to out-of-source if desired)
        cmd = ["cmake", "."]
        print(f"Found CMakeLists.txt, running: {' '.join(cmd)} (cwd={p})")
        with pushd(p):
            subprocess.run(cmd, check=True)
        return

    print(f"No configure step detected in {p}; skipping configure.")


def generate_compile_commands(project_path: str, build_cmd: str = "make", output: str = "compile_commands.json") -> Path:
    """
    Drive CodeChecker to generate `compile_commands.json` for the given project.

    It will perform:
      1) `make clean` (if `make` is available)
      2) invoke CodeChecker to record the build and emit `compile_commands.json`

    Returns the Path to the generated compile_commands.json.
    """
    p = Path(project_path)
    if not p.exists():
        raise FileNotFoundError(f"project_path not found: {project_path}")

    # run make clean if make is available and build_cmd contains 'make' or make exists
    make_exe = shutil.which("make")
    if make_exe:
        try:
            print(f"Running: make clean (cwd={p})")
            with pushd(p):
                subprocess.run(["make", "clean"], check=True)
        except subprocess.CalledProcessError as e:
            print(f"make clean failed: {e}; continuing")

    # find CodeChecker
    codechecker = shutil.which("CodeChecker") or shutil.which("codechecker")
    if not codechecker:
        raise FileNotFoundError("CodeChecker executable not found in PATH; please install CodeChecker or add it to PATH")

    # Build the CodeChecker log command. Use shell=False where possible.
    # The user typically runs: CodeChecker log --build "make" -o compile_commands.json
    # Note: some CodeChecker versions accept -o or --output; support both forms by using --output.
    cmd = [codechecker, "log", "--build", build_cmd, "--output", output]

    print(f"Running CodeChecker to record build: {' '.join(cmd)} (cwd={p})")
    with pushd(p):
        subprocess.run(cmd, check=True)

    out_path = p / output
    if not out_path.exists():
        raise FileNotFoundError(f"Expected output not found: {out_path}")

    print(f"Generated {out_path}")
    return out_path


def main():
    """CLI helper to configure a project and generate compile_commands.json.

    Usage:
      python scripts/compile.py /path/to/project [--configure-cmd "./configure --prefix=..."] [--build-cmd "make"]
    """
    import argparse

    parser = argparse.ArgumentParser(description="Configure and generate compile_commands.json using CodeChecker")
    parser.add_argument("project", help="Path to the project root")
    parser.add_argument("--configure-cmd", help="Custom configure command to run (string, shell) ")
    parser.add_argument("--build-cmd", default="make", help="Build command to pass to CodeChecker (default: make)")
    parser.add_argument("--output", default="compile_commands.json", help="Output filename (default: compile_commands.json)")

    args = parser.parse_args()

    try:
        configure_project(args.project, configure_cmd=args.configure_cmd)
    except Exception as e:
        print(f"configure_project failed: {e}")
        sys.exit(2)

    try:
        out = generate_compile_commands(args.project, build_cmd=args.build_cmd, output=args.output)
        print(f"Success: compile_commands at {out}")
    except Exception as e:
        print(f"generate_compile_commands failed: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()


