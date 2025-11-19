"""Compare intermediate JSON outputs and produce detailed JSON and CSV summaries.

This comparator expects the intermediate JSON files produced by
`scripts/extractor.py` (one JSON per project). Given a directory of
ground-truth intermediate files and a directory of candidate
intermediate files, it matches projects by the JSON payload `project`
field and computes true-positives (TP), false-positives (FP) and
false-negatives (FN) by exact matching of (file_path, checker, line,
message).

Outputs
	- Detailed JSON per project with per-checker tp/fp/fn counts and
		detailed lists of entries for manual inspection.
	- A CSV summary (one row per project and a final 'all' row) with
		columns: project_name,tp,fp,fn,precision,recall.

Usage (CLI):
	python comparator.py --groundtruth <dir_or_file> --compare <dir_or_file> --outdir <dir>

The script is intentionally conservative and uses the `project` key
inside each intermediate JSON to match projects. If the file does not
contain a `project` key, the filename (basename without extension) is
used as a fallback.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple


logger = logging.getLogger("comparator")


def find_json_files(path: str) -> List[str]:
	"""Return a list of .json files under `path` (file or directory)."""
	if os.path.isfile(path):
		return [path] if path.lower().endswith(".json") else []
	out: List[str] = []
	for root, _, files in os.walk(path):
		for fn in files:
			if fn.lower().endswith(".json"):
				out.append(os.path.join(root, fn))
	return out


def load_intermediate(path: str) -> Dict:
	"""Load an intermediate JSON file and return its payload dict.

	If loading fails, raises an exception.
	"""
	with open(path, "r", encoding="utf-8") as rf:
		return json.load(rf)


def entries_to_set(files_map: Dict[str, List[Dict]]) -> Set[Tuple[str, str, Optional[int], str]]:
	"""Convert files->entries mapping to a set of keys for comparison.

	Each key is (file_path, checker, line, message) where line or
	message may be None/empty.
	"""
	s: Set[Tuple[str, str, Optional[int], str]] = set()
	for fp, entries in files_map.items():
		for e in entries:
			checker = e.get("checker", "")
			message = e.get("message", "") or ""
			line = e.get("line") if "line" in e else None
			s.add((fp, checker, line, message))
	return s


def summarize_project(gt_payload: Dict, cmp_payload: Dict) -> Dict:
	"""Compare two intermediate payloads and return a detailed summary.

	Returned structure contains overall summary and per-checker details.
	"""
	project = gt_payload.get("project") or cmp_payload.get("project") or "unknown"
	gt_files = gt_payload.get("files", {})
	cmp_files = cmp_payload.get("files", {})

	gt_set = entries_to_set(gt_files)
	cmp_set = entries_to_set(cmp_files)

	tp = gt_set & cmp_set
	fp = cmp_set - gt_set
	fn = gt_set - cmp_set

	def make_detail(item: Tuple[str, str, Optional[int], str]) -> Dict:
		fp, checker, line, message = item
		return {"file": fp, "checker": checker, "line": line, "message": message}

	by_checker: Dict[str, Dict] = {}
	# collect entries per checker
	for item in tp:
		checker = item[1]
		rec = by_checker.setdefault(checker, {"tp": 0, "fp": 0, "fn": 0, "tp_details": [], "fp_details": [], "fn_details": []})
		rec["tp"] += 1
		rec["tp_details"].append(make_detail(item))

	for item in fp:
		checker = item[1]
		rec = by_checker.setdefault(checker, {"tp": 0, "fp": 0, "fn": 0, "tp_details": [], "fp_details": [], "fn_details": []})
		rec["fp"] += 1
		rec["fp_details"].append(make_detail(item))

	for item in fn:
		checker = item[1]
		rec = by_checker.setdefault(checker, {"tp": 0, "fp": 0, "fn": 0, "tp_details": [], "fp_details": [], "fn_details": []})
		rec["fn"] += 1
		rec["fn_details"].append(make_detail(item))

	summary = {"tp": len(tp), "fp": len(fp), "fn": len(fn)}

	return {
		"project": project,
		"summary": summary,
		"by_checker": by_checker,
		"generated_at": datetime.utcnow().isoformat() + "Z",
	}


def write_detailed_json(out_dir: str, project: str, summary: Dict) -> str:
	os.makedirs(out_dir, exist_ok=True)
	filename = f"{project}.comparison.json"
	out_path = os.path.join(out_dir, filename)
	# avoid clobber if exists
	if os.path.exists(out_path):
		ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
		out_path = os.path.join(out_dir, f"{project}.comparison.{ts}.json")
	with open(out_path, "w", encoding="utf-8") as of:
		json.dump(summary, of, ensure_ascii=False, indent=2)
	return out_path


def write_csv_summary(out_dir: str, rows: Iterable[Tuple[str, int, int, int, Optional[str]]]) -> str:
	os.makedirs(out_dir, exist_ok=True)
	date = datetime.utcnow().strftime("%Y%m%d")
	out_path = os.path.join(out_dir, f"{date}.csv")
	totals = {"tp": 0, "fp": 0, "fn": 0}

	with open(out_path, "w", encoding="utf-8", newline="") as cf:
		writer = csv.writer(cf)
		# Add analysis_time column (ISO UTC). Rows are expected as
		# (project, tp, fp, fn, analysis_time) where analysis_time may be None.
		writer.writerow(["project_name", "tp", "fp", "fn", "analysis_time", "precision", "recall"])
		for project, tp, fp, fn, analysis_time in rows:
			totals["tp"] += tp
			totals["fp"] += fp
			totals["fn"] += fn
			prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
			rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
			# Ensure we have an ISO timestamp; fallback to current time if missing
			atime = analysis_time if analysis_time else (datetime.utcnow().isoformat() + "Z")
			writer.writerow([project, tp, fp, fn, atime, f"{prec:.4f}", f"{rec:.4f}"])

	# final aggregate row
	ttp = totals["tp"]
	tfp = totals["fp"]
	tfn = totals["fn"]
	tprec = ttp / (ttp + tfp) if (ttp + tfp) > 0 else 0.0
	trec = ttp / (ttp + tfn) if (ttp + tfn) > 0 else 0.0
	# Use current time for aggregate row
	agg_time = datetime.utcnow().isoformat() + "Z"
	writer.writerow(["all", ttp, tfp, tfn, agg_time, f"{tprec:.4f}", f"{trec:.4f}"])

	return out_path


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
	p = argparse.ArgumentParser(description="Compare intermediate JSONs to a groundtruth and produce summaries")
	p.add_argument("--groundtruth", required=True, help="Path to groundtruth intermediate JSON file or directory")
	p.add_argument("--compare", required=True, help="Path to candidate intermediate JSON file or directory to compare against groundtruth")
	p.add_argument("--outdir", required=True, help="Directory where comparison JSON and CSV will be written")
	p.add_argument("--verbose", action="store_true", help="Enable verbose logging")
	return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
	args = parse_args(argv)
	logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

	gt_files = find_json_files(args.groundtruth)
	cmp_files = find_json_files(args.compare)

	# Create a dated results directory under the outdir so results are
	# organized by date (e.g. <outdir>/20251117/)
	date_dir_name = datetime.utcnow().strftime("%Y%m%d")
	results_dir = os.path.join(args.outdir, date_dir_name)

	if not gt_files:
		logger.error("No groundtruth intermediate JSON files found under %s", args.groundtruth)
		return 2
	if not cmp_files:
		logger.error("No candidate intermediate JSON files found under %s", args.compare)
		return 2

	# load all cmp payloads keyed by project name
	cmp_map: Dict[str, Tuple[str, Dict]] = {}
	for p in cmp_files:
		try:
			payload = load_intermediate(p)
			project = payload.get("project") or os.path.splitext(os.path.basename(p))[0]
			# if multiple files for same project, prefer last-loaded (timestamped files)
			cmp_map[project] = (p, payload)
		except Exception:
			logger.exception("Failed to load candidate intermediate %s", p)

	rows: List[Tuple[str, int, int, int]] = []
	written_files: List[str] = []

	for g in gt_files:
		try:
			gt_payload = load_intermediate(g)
			project = gt_payload.get("project") or os.path.splitext(os.path.basename(g))[0]
			if project not in cmp_map:
				logger.warning("No matching candidate intermediate found for project %s (groundtruth file %s)", project, g)
				# still compute summary against empty candidate
				cmp_payload = {"project": project, "files": {}}
			else:
				cmp_payload = cmp_map[project][1]

			summary = summarize_project(gt_payload, cmp_payload)
			out_json = write_detailed_json(results_dir, project, summary)
			written_files.append(out_json)
			s = summary.get("summary", {})
			# Prefer analysis timing from the candidate intermediate payload
			# written by extractor: payload.metadata.timing.{end_timestamp,start_timestamp}
			analysis_time = None
			try:
				timing = None
				if isinstance(cmp_payload, dict):
					timing = cmp_payload.get("metadata", {}).get("timing", {})
				if timing and isinstance(timing, dict):
					end_ts = timing.get("end_timestamp") or timing.get("end_time")
					start_ts = timing.get("start_timestamp") or timing.get("start_time")
					chosen = end_ts or start_ts
					if isinstance(chosen, (int, float)):
						analysis_time = datetime.utcfromtimestamp(float(chosen)).isoformat() + "Z"
					elif isinstance(chosen, str) and chosen:
						# already a string; use as-is
						analysis_time = chosen
			except Exception:
				analysis_time = None

			# Fallbacks: comparator-generated timestamp in summary, then current time
			if not analysis_time:
				analysis_time = summary.get("generated_at") or (datetime.utcnow().isoformat() + "Z")

			rows.append((project, int(s.get("tp", 0)), int(s.get("fp", 0)), int(s.get("fn", 0)), analysis_time))
			logger.info("Wrote detailed comparison for %s -> %s", project, out_json)
		except Exception:
			logger.exception("Failed to process groundtruth file %s", g)

	csv_path = write_csv_summary(results_dir, rows)
	logger.info("Wrote CSV summary to %s", csv_path)
	# print outputs for scripting convenience
	print(json.dumps({"detailed_files": written_files, "csv": csv_path}, ensure_ascii=False))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
