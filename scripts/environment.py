# 源码环境构建脚本
# 功能一，构建目录，将sources中的源码解压到对应目录中
# 功能二，注释文件（从配置文件中读取信息，将指定项目的指定文件添加"nse0_"前缀）
# 功能三，恢复文件 去除指定项目中所有被注释的文件前缀"nse0_"

from pathlib import Path
import shutil
import os
from typing import Optional, Union, Dict
import zipfile
import tarfile
import tempfile


def build_dir(base_dir: Optional[Union[str, Path]] = None, force: bool = False) -> Dict[str, Path]:
    """
    Create the repository top-level directories described in README.

    Directories created (under base_dir):
      - projects
      - summaries
      - reports
      - intermediates
      - results

    Parameters
    - base_dir: path where to create the directories. If None, uses repository root
      (assumed to be two levels up from this file: project_root/).
    - force: if True, existing directories will be removed and recreated. If False,
      existing directories are left untouched.

    Returns a dict mapping directory name to its Path object.
    """
    # determine base directory (default: repo root, parent of scripts/)
    if base_dir is None:
        # this file is scripts/environment.py -> parent = scripts, parent.parent = repo root
        base = Path(__file__).resolve().parent.parent
    else:
        base = Path(base_dir).resolve()

    targets = ["projects", "summaries", "reports", "intermediates", "results"]

    # subdirectory layout according to README
    subdirs = {
        "projects": ["baseline", "groundtruth"],
        "summaries": ["sa", "llm"],
        # summaries/llm has its own children
        "summaries/llm": ["taint", "memory"],
        "reports": ["groundtruth", "baseline", "method"],
        "intermediates": ["groundtruth", "baseline", "method"],
        # results has no mandated children
    }

    created: Dict[str, Path] = {}

    # ensure base exists
    base.mkdir(parents=True, exist_ok=True)

    for name in targets:
        path = base / name
        # (re)create top-level directory
        if path.exists():
            if force:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                path.mkdir(parents=True, exist_ok=True)
                print(f"Re-created directory: {path}")
            else:
                print(f"Exists, skipped: {path}")
        else:
            path.mkdir(parents=True, exist_ok=True)
            print(f"Created directory: {path}")

        created[name] = path

        # create subdirectories if specified
        key = name
        if key in subdirs:
            for sub in subdirs[key]:
                subpath = path / sub
                if subpath.exists():
                    if force:
                        if subpath.is_dir():
                            shutil.rmtree(subpath)
                        else:
                            subpath.unlink()
                        subpath.mkdir(parents=True, exist_ok=True)
                        print(f"Re-created subdirectory: {subpath}")
                    else:
                        print(f"Exists, skipped subdirectory: {subpath}")
                else:
                    subpath.mkdir(parents=True, exist_ok=True)
                    print(f"Created subdirectory: {subpath}")

                created[f"{name}/{sub}"] = subpath

                # special-case: summaries/llm children
                if f"{name}/{sub}" in subdirs:
                    for sub2 in subdirs[f"{name}/{sub}"]:
                        sub2path = subpath / sub2
                        if sub2path.exists():
                            if force:
                                if sub2path.is_dir():
                                    shutil.rmtree(sub2path)
                                else:
                                    sub2path.unlink()
                                sub2path.mkdir(parents=True, exist_ok=True)
                                print(f"Re-created subdirectory: {sub2path}")
                            else:
                                print(f"Exists, skipped subdirectory: {sub2path}")
                        else:
                            sub2path.mkdir(parents=True, exist_ok=True)
                            print(f"Created subdirectory: {sub2path}")

                        created[f"{name}/{sub}/{sub2}"] = sub2path

    return created


def unzip_project(zip_file: Union[str, Path], dest_dir: Union[str, Path], unzip_name: Optional[str] = None,
                  overwrite: bool = False) -> Path:
    """
    Extract an archive into `dest_dir` and optionally rename the top-level folder.

    Parameters
    - zip_file: path to archive (.zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz)
    - dest_dir: directory where the project folder should be placed
    - unzip_name: optional new folder name for the extracted project. If not provided,
      the function will use the archive's single top-level folder name (if present) or
      the archive stem.
    - overwrite: if True and the target folder exists, it will be removed before moving
      the extracted files into place. If False and target exists, a FileExistsError is raised.

    Returns the Path to the final project directory.
    """
    zip_path = Path(zip_file)
    dest = Path(dest_dir)

    if not zip_path.exists():
        raise FileNotFoundError(f"Archive not found: {zip_path}")

    dest.mkdir(parents=True, exist_ok=True)

    # create temporary extraction dir
    tmpdir = Path(tempfile.mkdtemp(prefix="unzip_tmp_", dir=str(dest)))
    try:
        extracted_root = None

        # try zip
        if zipfile.is_zipfile(zip_path):
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmpdir)
        else:
            # try tar
            try:
                with tarfile.open(zip_path, 'r:*') as tf:
                    tf.extractall(tmpdir)
            except tarfile.ReadError:
                # fallback: attempt zip extraction (may raise)
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(tmpdir)

        # Inspect tmpdir to find the top-level extracted folder(s)
        children = [p for p in tmpdir.iterdir() if not p.name.startswith('.')]

        if len(children) == 1 and children[0].is_dir():
            extracted_root = children[0]
        else:
            # multiple entries -> treat tmpdir as root for contents
            extracted_root = tmpdir

        # decide final name
        if unzip_name:
            final_dir = dest / unzip_name
        else:
            if extracted_root is tmpdir:
                final_dir = dest / zip_path.stem
            else:
                final_dir = dest / extracted_root.name

        if final_dir.exists():
            if overwrite:
                if final_dir.is_dir():
                    shutil.rmtree(final_dir)
                else:
                    final_dir.unlink()
            else:
                raise FileExistsError(f"Destination already exists: {final_dir}")

        # move extracted content into final_dir
        if extracted_root is tmpdir:
            # need to create final_dir and move all contents
            final_dir.mkdir(parents=True, exist_ok=True)
            for item in tmpdir.iterdir():
                # move each child into final_dir
                target = final_dir / item.name
                shutil.move(str(item), str(target))
        else:
            # move the single directory to final location
            shutil.move(str(extracted_root), str(final_dir))

        print(f"Extracted {zip_path} -> {final_dir}")
        return final_dir
    finally:
        # cleanup tmpdir if it still exists
        try:
            if tmpdir.exists():
                # if tmpdir is empty, remove; else remove tree (safe because we've moved contents)
                shutil.rmtree(tmpdir)
        except Exception:
            pass

def unzip_source(base_dir: Optional[Union[str, Path]] = None, overwrite: bool = False):
    """
    Extract known archives from the `sources/` directory into projects/groundtruth
    and projects/baseline.

    Parameters:
      - base_dir: repository root (optional, defaults to this repo root)
      - overwrite: if True, existing project dirs will be overwritten when extracting
    """
    if base_dir is None:
        base = Path(__file__).resolve().parent.parent
    else:
        base = Path(base_dir).resolve()

    sources_dir = base / "sources"
    if not sources_dir.exists():
        raise FileNotFoundError(f"sources directory not found: {sources_dir}")

    # mapping from archive filename -> desired extracted folder name
    mapping = {
        "binutils-2.29.tar.gz": "binutils",
        "openssl-3.0.0.tar.gz": "openssl",
        "sqlite-version-3.32.0.tar.gz": "sqlite3",
    }

    projects_base = base / "projects"
    projects_ground = projects_base / "groundtruth"
    projects_base_dir = projects_base / "baseline"

    # ensure target directories exist (don't force recreate here)
    projects_ground.mkdir(parents=True, exist_ok=True)
    projects_base_dir.mkdir(parents=True, exist_ok=True)

    results = {}

    for archive_name, project_name in mapping.items():
        archive_path = sources_dir / archive_name
        if not archive_path.exists():
            print(f"Warning: archive not found, skipping: {archive_path}")
            continue

        # extract to groundtruth
        try:
            print(f"Extracting {archive_path.name} -> projects/groundtruth/{project_name}")
            gt_dir = unzip_project(archive_path, projects_ground, unzip_name=project_name, overwrite=overwrite)
            results[f"groundtruth/{project_name}"] = gt_dir
        except Exception as e:
            print(f"Failed to extract {archive_path} to groundtruth: {e}")

        # extract to baseline
        try:
            print(f"Extracting {archive_path.name} -> projects/baseline/{project_name}")
            bl_dir = unzip_project(archive_path, projects_base_dir, unzip_name=project_name, overwrite=overwrite)
            results[f"baseline/{project_name}"] = bl_dir
        except Exception as e:
            print(f"Failed to extract {archive_path} to baseline: {e}")

    return results


def comment_file():
    """
    Rename specified source files by adding a prefix (default "nse0_") to mark them as commented/out.

    Reads `configs/config.json` and looks for the `baseline` section. Expected format:
      "baseline": [ { "project_name": ["path/to/file.c", ...] }, ... ]

    For each listed file, the function will look under `projects/baseline/<project_name>/` and
    rename the file by prefixing its basename with `nse0_` unless it's already prefixed.

    Returns a dict mapping original paths to new paths for files that were renamed.
    """
    import json

    base = Path(__file__).resolve().parent.parent
    cfg_path = base / "configs" / "config.json"
    if not cfg_path.exists():
        raise FileNotFoundError(f"config.json not found at {cfg_path}")

    with cfg_path.open('r', encoding='utf-8') as f:
        cfg = json.load(f)

    baseline_list = cfg.get("baseline", [])
    results = {}

    projects_base = base / "projects" / "baseline"
    if not projects_base.exists():
        raise FileNotFoundError(f"projects/baseline directory not found: {projects_base}")

    prefix = "nse0_"

    for item in baseline_list:
        # each item is expected to be a dict with a single key: project name
        if not isinstance(item, dict):
            print(f"Skipping invalid baseline entry (not a dict): {item}")
            continue
        for project, files in item.items():
            proj_dir = projects_base / project
            if not proj_dir.exists():
                print(f"Warning: project directory not found, skipping project {project}: {proj_dir}")
                continue
            if not isinstance(files, list):
                print(f"Skipping invalid file list for project {project}: {files}")
                continue

            for rel in files:
                src_path = proj_dir / rel
                if not src_path.exists():
                    print(f"Warning: file to comment not found, skipping: {src_path}")
                    continue

                # compute target name in same directory
                parent = src_path.parent
                old_name = src_path.name
                if old_name.startswith(prefix):
                    print(f"Already prefixed, skipped: {src_path}")
                    continue

                new_name = prefix + old_name
                target = parent / new_name
                if target.exists():
                    print(f"Target already exists, skipping rename: {target}")
                    continue

                try:
                    src_path.rename(target)
                    results[str(src_path)] = str(target)
                    print(f"Renamed: {src_path} -> {target}")
                except Exception as e:
                    print(f"Failed to rename {src_path} -> {target}: {e}")

    return results


def recover_file():
    """
    Recover files previously 'commented' by removing the prefix `nse0_` from filenames
    under `projects/baseline` (recursively). If the target filename (without prefix)
    already exists, the file is skipped and a warning is printed.

    Returns a dict mapping original (prefixed) paths to recovered (unprefixed) paths.
    """
    base = Path(__file__).resolve().parent.parent
    projects_base = base / "projects" / "baseline"
    if not projects_base.exists():
        raise FileNotFoundError(f"projects/baseline directory not found: {projects_base}")

    prefix = "nse0_"
    results = {}

    for root, dirs, files in os.walk(projects_base):
        root_path = Path(root)
        for fname in files:
            if not fname.startswith(prefix):
                continue
            prefixed = root_path / fname
            unpref_name = fname[len(prefix):]
            target = root_path / unpref_name
            if target.exists():
                print(f"Target exists, skipping recovery: {target}")
                continue
            try:
                prefixed.rename(target)
                results[str(prefixed)] = str(target)
                print(f"Recovered: {prefixed} -> {target}")
            except Exception as e:
                print(f"Failed to recover {prefixed} -> {target}: {e}")

    return results


def main():
    """Simple CLI to exercise environment functions.

    Usage examples (run from project root):
      python scripts/environment.py --mode build --force
      python scripts/environment.py --mode build
      python scripts/environment.py --mode unzip
    """
    import argparse

    parser = argparse.ArgumentParser(description="Environment helper for new_summary_expriment")
    parser.add_argument("--mode", choices=["build", "unzip", "comment", "recover", "all"], default="build",
                        help="Which action to run")
    parser.add_argument("--force", action="store_true", help="Force recreate directories when applicable")
    parser.add_argument("--base", help="Base directory to operate on (defaults to repo root)")

    args = parser.parse_args()

    base = args.base if args.base else None

    if args.mode == "build":
        mapping = build_dir(base_dir=base, force=args.force)
        print("Resulting directories:")
        for k, v in mapping.items():
            print(f"  {k}: {v}")
    elif args.mode == "unzip":
        # run unzip flow; pass base and overwrite according to CLI args
        try:
            results = unzip_source(base_dir=base, overwrite=args.force)
            print("Unzip completed. Results:")
            for k, v in results.items():
                print(f"  {k}: {v}")
        except Exception as e:
            print(f"unzip_source failed: {e}")
    elif args.mode == "comment":
        try:
            res = comment_file()
            print("Commenting completed. Renamed files:")
            for k, v in res.items():
                print(f"  {k} -> {v}")
        except Exception as e:
            print(f"comment_file failed: {e}")
    elif args.mode == "recover":
        try:
            res = recover_file()
            print("Recover completed. Restored files:")
            for k, v in res.items():
                print(f"  {k} -> {v}")
        except Exception as e:
            print(f"recover_file failed: {e}")
    elif args.mode == "all":
        summary = {}
        # 1) build directories
        try:
            mapping = build_dir(base_dir=base, force=args.force)
            summary['build'] = mapping
            print("Build completed.")
        except Exception as e:
            print(f"build_dir failed: {e}")
            summary['build_error'] = str(e)

        # 2) unzip sources
        try:
            unzip_results = unzip_source(base_dir=base, overwrite=args.force)
            summary['unzip'] = unzip_results
            print("Unzip completed.")
        except Exception as e:
            print(f"unzip_source failed: {e}")
            summary['unzip_error'] = str(e)

        # 3) comment baseline files
        # try:
        #     comment_results = comment_file()
        #     summary['comment'] = comment_results
        #     print("Comment completed.")
        # except Exception as e:
        #     print(f"comment_file failed: {e}")
        #     summary['comment_error'] = str(e)

        # print("All done. Summary keys:")
        # for k in summary.keys():
        #     print(f"  {k}")


if __name__ == "__main__":
    main()
