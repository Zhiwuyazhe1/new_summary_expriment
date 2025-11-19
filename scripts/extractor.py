"""Extractor for CodeChecker plist reports.

This script reads one or more CodeChecker/clangsa plist files (XML plist
format) plus optional analysis metadata written by
`scripts/codechecker_driver.py` (analysis_time.json) and produces a
single intermediate JSON file per project containing per-file reports
and metadata.

Output shape (example):

{
  "project": "project_name",
  "files": {
  "/abs/path/to/file.c": [
    {"checker": "optin.portability.UnixAPI", "message": "...", "line": 193},
    ...
  ],
  ...
  },
  "metadata": { ... }
}

The script is intentionally permissive about file paths: it writes the
file paths as they appear in the plist. If you want relative paths, you
can post-process the JSON file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import sys
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple


logger = logging.getLogger("extractor")


def find_plist_files(path: str) -> List[str]:
  """Return a list of .plist files. If `path` is a file it will be
  returned (if it ends with .plist), otherwise the directory is
  walked recursively.
  """
  if os.path.isfile(path):
    return [path] if path.lower().endswith(".plist") else []

  plist_paths: List[str] = []
  for root, _, files in os.walk(path):
    for fn in files:
      if fn.lower().endswith(".plist"):
        plist_paths.append(os.path.join(root, fn))
  return plist_paths


def extract_plist(plist_path: str) -> Tuple[Dict[str, List[Dict]], Dict]:
  """Parse a single plist file and return (reports_by_file, plist_metadata).

  reports_by_file maps file-path -> list of {checker,message,line}.
  plist_metadata contains top-level metadata found in the plist.
  """
  reports: Dict[str, List[Dict]] = {}
  metadata: Dict = {}

  try:
    with open(plist_path, "rb") as f:
      doc = plistlib.load(f)
  except Exception as e:
    logger.exception("Failed to load plist %s: %s", plist_path, e)
    return reports, metadata

  files = doc.get("files", []) or []
  diagnostics = doc.get("diagnostics", []) or []
  metadata = doc.get("metadata", {}) or {}

  for diag in diagnostics:
    checker = diag.get("check_name") or diag.get("category") or ""
    # CodeChecker/clang-sa uses 'description' for the human message
    message = diag.get("description") or diag.get("message") or ""

    location = diag.get("location") or {}
    line = location.get("line")
    file_index = location.get("file")

    file_path: Optional[str] = None
    if isinstance(file_index, int) and 0 <= file_index < len(files):
      file_path = files[file_index]

    # Fallback: some diagnostics put path under 'path' structure
    if not file_path:
      # try to read from top-level or nested keys
      file_path = diag.get("file_path") or diag.get("path")
      if isinstance(file_path, list):
        # sometimes path is an array of dicts, safe-guard
        file_path = None

    if not file_path:
      # put under unknown key
      file_path = "<unknown>"

    entry = {"checker": checker, "message": message, "line": line}
    reports.setdefault(file_path, []).append(entry)

  return reports, metadata


def extract_metadata_from_report_dir(report_dir: str) -> Dict:
  """Try to extract analysis timing metadata from analysis_time.json
  written by `codechecker_driver.py` and return a dict with any found
  values. If the file is missing, return an empty dict.
  """
  # Look for analysis_time.json in several sensible locations:
  #  - directly under report_dir
  #  - in any immediate subdirectory (CodeChecker sometimes nests outputs)
  #  - in parent directories (caller might pass a subpath)
  candidates: List[str] = []

  # direct path: prefer analysis_time.json but also consider metadata.json
  candidates.append(os.path.join(report_dir, "analysis_time.json"))
  candidates.append(os.path.join(report_dir, "metadata.json"))

  # immediate children
  try:
    if os.path.isdir(report_dir):
      for name in os.listdir(report_dir):
        child = os.path.join(report_dir, name)
        if os.path.isdir(child):
          candidates.append(os.path.join(child, "analysis_time.json"))
  except Exception:
    # ignore listing errors
    pass

  # search recursively but limit depth to avoid long scans
  try:
    for root, dirs, files in os.walk(report_dir):
      # limit search depth by stopping when path depth exceeds report_dir + 2
      rel = os.path.relpath(root, report_dir)
      # rel == '.' for root
      if rel != '.' and rel.count(os.sep) > 2:
        # don't recurse deeper
        dirs[:] = []
        continue
      if 'analysis_time.json' in files:
        candidates.append(os.path.join(root, 'analysis_time.json'))
  except Exception:
    pass

  # also check up to two parent dirs
  try:
    cur = report_dir
    for _ in range(2):
      cur = os.path.dirname(cur)
      if not cur:
        break
      candidates.append(os.path.join(cur, 'analysis_time.json'))
  except Exception:
    pass

  seen = set()
  for meta_path in candidates:
    if not meta_path or meta_path in seen:
      continue
    seen.add(meta_path)
    if os.path.isfile(meta_path):
      try:
        with open(meta_path, 'r', encoding='utf-8') as mf:
          data = json.load(mf)

        # 1) If the file already contains elapsed_seconds use it directly
        if isinstance(data, dict) and 'elapsed_seconds' in data:
          timing = {
            'start_timestamp': data.get('start_timestamp'),
            'end_timestamp': data.get('end_timestamp'),
            'elapsed_seconds': float(data.get('elapsed_seconds')) if data.get('elapsed_seconds') is not None else None,
          }
          return timing

        # 2) Some toolings write timestamps under a `timestamps` key (e.g. metadata.json)
        def _find_timestamps(obj):
          # recursively search for a dict that contains a `timestamps` mapping
          if isinstance(obj, dict):
            if 'timestamps' in obj and isinstance(obj['timestamps'], dict):
              t = obj['timestamps']
              if 'begin' in t and 'end' in t:
                return (t.get('begin'), t.get('end'))
            # also allow top-level keys named begin/end
            if 'begin' in obj and 'end' in obj:
              return (obj.get('begin'), obj.get('end'))
            for v in obj.values():
              res = _find_timestamps(v)
              if res:
                return res
          elif isinstance(obj, list):
            for item in obj:
              res = _find_timestamps(item)
              if res:
                return res
          return None

        ts = _find_timestamps(data)
        if ts:
          try:
            begin, end = float(ts[0]), float(ts[1])
            elapsed = end - begin
            timing = {
              'start_timestamp': begin,
              'end_timestamp': end,
              'elapsed_seconds': float(elapsed),
            }
            return timing
          except Exception:
            # fallthrough to other heuristics
            pass

        # 3) As a last attempt, if data is a dict with nested keys like
        #    {"analysis": {"timing": {...}}} try to locate elapsed_seconds
        def _find_elapsed(obj):
          if isinstance(obj, dict):
            if 'elapsed_seconds' in obj:
              return obj.get('elapsed_seconds')
            for v in obj.values():
              res = _find_elapsed(v)
              if res is not None:
                return res
          elif isinstance(obj, list):
            for item in obj:
              res = _find_elapsed(item)
              if res is not None:
                return res
          return None

        elapsed_val = _find_elapsed(data)
        if elapsed_val is not None:
          try:
            ev = float(elapsed_val)
            return {'elapsed_seconds': ev}
          except Exception:
            pass

      except Exception:
        logger.exception('Failed to read timing metadata at %s', meta_path)

  return {}


  # Fallback: try to parse elapsed seconds from common debug/log files
  # produced by codechecker_driver (analysis_debug.txt, codechecker_stdout.txt, codechecker_stderr.txt)
  debug_candidates = [
    os.path.join(report_dir, 'analysis_debug.txt'),
    os.path.join(report_dir, 'codechecker_stdout.txt'),
    os.path.join(report_dir, 'codechecker_stderr.txt'),
  ]
  # also check immediate subdirs for debug files
  try:
    if os.path.isdir(report_dir):
      for name in os.listdir(report_dir):
        child = os.path.join(report_dir, name)
        if os.path.isdir(child):
          debug_candidates.append(os.path.join(child, 'analysis_debug.txt'))
          debug_candidates.append(os.path.join(child, 'codechecker_stdout.txt'))
          debug_candidates.append(os.path.join(child, 'codechecker_stderr.txt'))
  except Exception:
    pass

  import re
  elapsed_re = re.compile(r'Elapsed seconds:\s*([0-9]+(?:\.[0-9]+)?)', re.IGNORECASE)
  for dpath in debug_candidates:
    try:
      if not dpath or not os.path.isfile(dpath):
        continue
      with open(dpath, 'r', encoding='utf-8', errors='ignore') as df:
        for line in df:
          m = elapsed_re.search(line)
          if m:
            try:
              val = float(m.group(1))
              return {'elapsed_seconds': val}
            except Exception:
              continue
    except Exception:
      # non-fatal; continue to next candidate
      continue

  return {}


def merge_reports(all_reports: List[Dict[str, List[Dict]]]) -> Dict[str, List[Dict]]:
  """Merge multiple per-plist reports into one mapping and deduplicate
  diagnostic entries.
  """
  merged: Dict[str, List[Dict]] = {}
  seen: Set[Tuple[str, str, Optional[int], str]] = set()

  for reports in all_reports:
    for file_path, entries in reports.items():
      for e in entries:
        checker = e.get("checker", "")
        message = e.get("message", "")
        line = e.get("line")
        key = (file_path, checker, line, message)
        if key in seen:
          continue
        seen.add(key)
        merged.setdefault(file_path, []).append({
          "checker": checker,
          "message": message,
          "line": line,
        })

  return merged


def generate_intermediate(reports_path: str, out_dir: str, project_name: Optional[str] = None, project_root: Optional[str] = None) -> str:
  """Generate an intermediate JSON file from reports_path and write it to
  out_dir. Returns the path to the written JSON file.
  """
  plist_paths = find_plist_files(reports_path)
  if not plist_paths:
    raise FileNotFoundError(f"No plist files found under {reports_path}")

  all_reports: List[Dict[str, List[Dict]]] = []
  plist_metadata: Dict = {}
  for p in plist_paths:
    r, m = extract_plist(p)
    all_reports.append(r)
    # Keep the first non-empty metadata we find
    if not plist_metadata and m:
      plist_metadata = m

  merged = merge_reports(all_reports)

  # If a project_root is provided, try to map absolute file paths to
  # paths relative to that project root. This makes the JSON easier to
  # compare across environments and aligns with the user's expectation
  # of seeing project-internal paths like 'crypto/pkcs7/pk7_doit.c'.
  if project_root:
    mapped_reports: Dict[str, List[Dict]] = {}
    for file_path, entries in merged.items():
      mapped = make_relative_path(file_path, project_root)
      mapped_reports.setdefault(mapped, []).extend(entries)

    # deduplicate per-file entries (simple local dedupe)
    deduped: Dict[str, List[Dict]] = {}
    for fp, entries in mapped_reports.items():
      seen_local: Set[Tuple[Optional[str], Optional[int], str]] = set()
      out_entries: List[Dict] = []
      for e in entries:
        key = (e.get('checker'), e.get('line'), e.get('message'))
        if key in seen_local:
          continue
        seen_local.add(key)
        out_entries.append(e)
      deduped[fp] = out_entries

    merged = deduped

  # Try to read analysis_time.json if present in the same directory as
  # the reports_path (or the parent if a file was passed).
  report_dir = reports_path if os.path.isdir(reports_path) else os.path.dirname(reports_path)
  timing_meta = extract_metadata_from_report_dir(report_dir)

  metadata = {
    "extracted_at": datetime.utcnow().isoformat() + "Z",
    "source_plists": len(plist_paths),
    "plist_metadata": plist_metadata,
    "timing": timing_meta,
  }

  def make_relative_path(abs_path: str, project_root: Optional[str]) -> str:
    if not project_root:
      return abs_path.replace('\\', '/')

    try:
      abs_p = os.path.abspath(abs_path)
      abs_root = os.path.abspath(project_root)
      rel = os.path.relpath(abs_p, abs_root)
      # If rel is outside (starts with ..) or remains absolute, fallback
      if not rel.startswith('..') and not os.path.isabs(rel):
        return rel.replace('\\', '/')
    except Exception:
      # Fall through to fallback strategies
      pass

    # Fallback: try to locate the project root basename in the path
    try:
      root_name = os.path.basename(os.path.abspath(project_root))
      idx = abs_path.rfind(root_name)
      if idx != -1:
        candidate = abs_path[idx + len(root_name):].lstrip('/\\')
        if candidate:
          return candidate.replace('\\', '/')
    except Exception:
      pass

    # As last resort return normalized absolute path
    return abs_path.replace('\\', '/')

  # if a project_root was provided via CLI we'll set it later in caller
  payload = {
    "project": project_name or os.path.basename(os.path.abspath(report_dir)) or "unknown",
    "files": merged,
    "metadata": metadata,
  }

  os.makedirs(out_dir, exist_ok=True)
  out_name = f"{payload['project']}.json"
  out_path = os.path.join(out_dir, out_name)
  # If the file already exists, append timestamp to avoid clobbering
  if os.path.exists(out_path):
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(out_dir, f"{payload['project']}.{ts}.json")

  with open(out_path, "w", encoding="utf-8") as of:
    json.dump(payload, of, ensure_ascii=False, indent=2)

  logger.info("Wrote intermediate JSON to %s", out_path)
  return out_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Extract CodeChecker plist reports into intermediate JSON")
  p.add_argument("--reports", required=True, help="Path to a plist file or directory containing plist files")
  p.add_argument("--outdir", required=True, help="Directory where intermediate JSON will be written")
  p.add_argument("--project-root", default=None, help="Project root directory to make file paths relative to")
  p.add_argument("--project", default=None, help="Optional project name to use in the intermediate file")
  p.add_argument("--verbose", action="store_true", help="Enable verbose logging")
  return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
  args = parse_args(argv)
  logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

  try:
    # We generate first, then if project_root provided we rewrite file paths
    out_path = generate_intermediate(args.reports, args.outdir, args.project, args.project_root)

    # Now post-process the generated file to relativize paths if requested
    if args.project_root:
      try:
        with open(out_path, 'r', encoding='utf-8') as rf:
          payload = json.load(rf)

        # apply relative mapping
        abs_root = args.project_root
        new_files: Dict[str, List[Dict]] = {}
        for fp, entries in payload.get('files', {}).items():
          # normalize existing file path
          mapped = fp
          # use same logic as make_relative_path inside generate_intermediate
          try:
            abs_fp = fp
            # If fp is already absolute on this OS, keep it; else keep as-is
            mapped = fp
            # Try os.path.relpath
            try:
              rel = os.path.relpath(abs_fp, abs_root)
              if not rel.startswith('..') and not os.path.isabs(rel):
                mapped = rel.replace('\\', '/')
              else:
                # fallback to basename search
                root_name = os.path.basename(os.path.abspath(abs_root))
                idx = abs_fp.rfind(root_name)
                if idx != -1:
                  candidate = abs_fp[idx + len(root_name):].lstrip('/\\')
                  if candidate:
                    mapped = candidate.replace('\\', '/')
            except Exception:
              pass
          except Exception:
            mapped = fp
          new_files[mapped] = entries

        payload['files'] = new_files
        # rewrite file (keep original filename)
        with open(out_path, 'w', encoding='utf-8') as wf:
          json.dump(payload, wf, ensure_ascii=False, indent=2)
        logger.info('Relativized file paths using project root %s', args.project_root)
      except Exception:
        logger.exception('Failed to relativize paths using project root %s', args.project_root)

    print(out_path)
    return 0
  except Exception as e:
    logger.exception("Failed to generate intermediate: %s", e)
    return 2


if __name__ == "__main__":
  raise SystemExit(main())
