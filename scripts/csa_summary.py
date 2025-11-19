"""
驱动 clang 对目标文件/目标函数进行分析，生成摘要（summary）。

用法概述：
- 模式：file 或 function
- file 模式下：读取 .c 文件，提取可能的顶层函数名列表（使用简单正则），然后对每个函数执行 clang 分析
- function 模式下：对指定的函数名在指定的源文件上执行分析

输出：clang 的 summary 会被写入到 `--summary-dir` 指定的目录，摘要文件命名为 <function_name>.json

注意：本脚本使用一个较为宽松的正则来识别 C 的函数定义，不能覆盖所有边界情况（宏生成的函数、复杂声明等）。如需更准确地解析，请使用 clang AST 或 ctags/ctags-exuberant 等工具。
"""

from __future__ import annotations

import argparse
import json
import shlex
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Set, Optional

__all__ = ["find_functions_in_c_file", "find_project_root", "build_clang_command", "run_analysis_for_function", "main"]

DEFAULT_CLANG_BIN = "/bigdata/huawei-proj/zqj/llvm-15.0.4/build/bin/clang-15"

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def find_functions_in_c_file(file_path: str) -> List[str]:
	"""从 C 源文件中尽可能准确地提取顶层函数名列表。

	说明/限制：
	- 使用简单的正则匹配形如 "return_type name(args) {" 的行，跳过以 ';' 结尾的函数声明。
	- 不能处理通过宏定义产生的函数名或非常规换行的声明。
	"""
	src = Path(file_path).read_text(encoding="utf-8", errors="ignore")

	# 一个简单的正则：匹配以开始（或换行）后，若干返回类型字符，然后捕获函数名，随后是括号参数，最后是左花括号
	# 这个正则尽力避免匹配函数声明（带分号）或指针声明里的小坑
	pattern = re.compile(r"^[\s\w\*\(\)]+\b([A-Za-z_][A-Za-z0-9_]*)\s*\([^;\)]*\)\s*\{", re.MULTILINE)

	matches = pattern.findall(src)
	# 结果中可能包含关键字（如 if, switch），需要过滤掉常见的非函数名
	filtered: List[str] = []
	blacklist = {"if", "for", "while", "switch", "return", "sizeof"}
	for name in matches:
		if name in blacklist:
			continue
		filtered.append(name)

	# 去重并保持顺序
	seen: Set[str] = set()
	result: List[str] = []
	for n in filtered:
		if n not in seen:
			seen.add(n)
			result.append(n)

	logger.info("Found %d candidate functions in %s", len(result), file_path)
	return result


def find_project_root(file_path: str) -> str:
	"""从给定源文件向上寻找项目根目录的启发式方法。

	搜索以下标记文件以判断项目根：.git, configure, Makefile, CMakeLists.txt, configure.ac
	如果未找到，则返回源文件所在目录。
	"""

	cur = Path(file_path).resolve().parent
	markers = {".git", "configure", "Makefile", "CMakeLists.txt", "configure.ac"}
	for parent in [cur] + list(cur.parents):
		try:
			entries = {p.name for p in parent.iterdir()}
		except PermissionError:
			continue
		if markers & entries:
			return str(parent)
	# fallback: directory containing the source file
	return str(cur)


def load_compile_commands(path: Path) -> List[dict]:
	try:
		with path.open('r', encoding='utf-8') as f:
			data = json.load(f)
	except Exception:
		logger.warning("Failed to read or parse compile_commands.json at %s", path)
		return []
	if not isinstance(data, list):
		logger.warning("compile_commands.json does not contain a list: %s", path)
		return []
	return data


def find_compile_commands_entry(entries: List[dict], src_file: str) -> Optional[dict]:
	# try exact match first, then basename match
	src_path = Path(src_file)
	# Normalize target absolute path for comparison
	try:
		tgt_abs = src_path.resolve()
	except Exception:
		tgt_abs = src_path

	# 1) exact resolved path or via entry 'directory'
	for e in entries:
		if 'file' not in e:
			continue
		entry_file = Path(e['file'])
		# if compile_commands provides directory, combine
		dir_field = e.get('directory')
		if dir_field:
			try:
				entry_full = (Path(dir_field) / entry_file).resolve()
			except Exception:
				entry_full = Path(dir_field) / entry_file
			if entry_full == tgt_abs:
				return e
		# try resolving entry file directly
		try:
			if entry_file.resolve() == tgt_abs:
				return e
		except Exception:
			pass

	# 2) try endswith match of the path (useful if compile_commands stores relative paths)
	for e in entries:
		if 'file' not in e:
			continue
		entry_file_str = str(e['file']).replace('\\', '/')
		tgt_str = str(src_path.as_posix())
		if tgt_str.endswith(entry_file_str) or entry_file_str.endswith(src_path.name):
			return e

	# 3) basename match as last resort
	for e in entries:
		if 'file' in e and Path(e['file']).name == src_path.name:
			return e

	return None


def tokenize_command(entry: dict) -> List[str]:
	if 'arguments' in entry and isinstance(entry['arguments'], list):
		return [str(x) for x in entry['arguments']]
	if 'command' in entry and isinstance(entry['command'], str):
		try:
			return shlex.split(entry['command'], posix=True)
		except Exception:
			return entry['command'].split()
	return []


def extract_relevant_flags(tokens: List[str], src_file: str) -> List[str]:
	keep: List[str] = []
	i = 0
	while i < len(tokens):
		t = tokens[i]
		# skip compiler binary and -c, -o
		if i == 0 and (Path(t).name.lower().startswith('clang') or Path(t).name.lower().startswith('gcc') or Path(t).endswith('.exe')):
			i += 1
			continue
		if t == '-c':
			i += 1
			continue
		if t == '-o':
			i += 2
			continue

		# skip the source file token if present
		if Path(t).as_posix() == Path(src_file).as_posix() or Path(t).name == Path(src_file).name:
			i += 1
			continue

		if t.startswith('-I') or t.startswith('-isystem') or t.startswith('-iquote') or t.startswith('-D') or t.startswith('-std'):
			keep.append(t)
			i += 1
			continue

		if t in ('-I', '-isystem', '-iquote', '-idirafter', '-iprefix'):
			if i + 1 < len(tokens):
				keep.append(t)
				keep.append(tokens[i + 1])
				i += 2
				continue

		i += 1
	return keep


def build_clang_command(clang_bin: str, function_name: str, file_path: str, summary_dir: str, extra_flags: Optional[List[str]] = None) -> List[str]:
	"""构建 clang 分析命令的参数列表（适合传递给 subprocess.run）。

	说明：不再显式注入 -I 头文件路径；运行前会切换到项目根目录以便 clang 在相对路径下寻找头文件。

	命令格式按需调整为：
	<clang> -analyze <source-file> -- <analyzer-args...>
	这样 clang-check/clang 可以正确识别要分析的源文件与后续的 -Xanalyzer 参数。
	"""

	# Start with analyzer invocation and optionally include any extracted compile flags
	cmd: List[str] = [clang_bin, "--analyze"]

	# place extra flags (includes/macros) right after --analyze so clang can preprocess correctly
	if extra_flags:
		cmd += extra_flags

	# Append analyzer options as -Xanalyzer <arg> pairs
	cmd += ["-Xanalyzer", "-analyzer-purge=none"]
	cmd += ["-Xanalyzer", "-analyzer-checker=alpha.core.DumpSummary"]
	# analyze-function needs to be passed through -Xanalyzer; follow with the function name
	cmd += ["-Xanalyzer", "-analyze-function", "-Xanalyzer", function_name]

	cmd += ["-Xanalyzer", "-analyzer-config", "-Xanalyzer", "clear-overlap-offset=false"]
	cmd += ["-Xanalyzer", "-analyzer-config", "-Xanalyzer", f"summary-dir={summary_dir}"]

	# finally append the source file to analyze
	cmd += [file_path]

	return cmd


def run_analysis_for_function(clang_bin: str, function_name: str, file_path: str, summary_dir: str, dry_run: bool = False, project_root: Optional[str] = None, compile_commands: Optional[str] = None) -> int:
	"""对单个函数触发 clang 分析。返回子进程退出码（0 表示成功）。

	该函数会确保 summary_dir 存在。
	"""
	Path(summary_dir).mkdir(parents=True, exist_ok=True)

	# Determine project root: prefer explicit override, otherwise heuristically find it
	if project_root:
		proj_root = project_root
	else:
		proj_root = find_project_root(file_path)
	# If compile_commands.json provided, try to extract include/macros from it for this file
	extra_flags: List[str] = []
	# If compile_commands.json provided, or if not provided attempt to auto-locate it under project root,
	# try to extract include/macros from it for this file
	ccp: Optional[Path] = None
	if compile_commands:
		ccp = Path(compile_commands)
	else:
		# try common locations under project root
		cand1 = Path(proj_root) / "compile_commands.json"
		cand2 = Path(proj_root) / "build" / "compile_commands.json"
		if cand1.exists():
			ccp = cand1
		elif cand2.exists():
			ccp = cand2

	if ccp:
		if ccp.exists():
			entries = load_compile_commands(ccp)
			entry = find_compile_commands_entry(entries, file_path)
			if entry:
				tokens = tokenize_command(entry)
				extra_flags = extract_relevant_flags(tokens, file_path)
				logger.info("Found compile_commands entry: %s", ccp)
				logger.info("Compile command tokens: %s", " ".join(tokens))
				logger.info("Extracted %d flags from %s: %s", len(extra_flags), ccp, " ".join(extra_flags))
			else:
				logger.info("No compile_commands entry matched for %s in %s", file_path, ccp)
		else:
			logger.info("compile_commands.json not found at %s", ccp)
	else:
		logger.debug("No compile_commands.json provided and none found under project root %s", proj_root)

	cmd = build_clang_command(clang_bin, function_name, file_path, summary_dir, extra_flags=extra_flags)

	logger.info("Running analysis for function '%s' in %s", function_name, file_path)
	if dry_run:
		logger.info("Dry run - project root: %s", proj_root)
		logger.info("Dry run - command: %s", " ".join(cmd))
		return 0

	try:
		# 运行外部命令并捕获输出；在项目根目录下执行以便 clang 能用相对 include 路径查找头文件
		completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=proj_root)
	except FileNotFoundError as e:
		logger.error("Clang binary not found: %s", clang_bin)
		return 127

	if completed.returncode != 0:
		logger.error("Analysis for %s failed (rc=%d). stderr:\n%s", function_name, completed.returncode, completed.stderr)
	else:
		logger.info("Analysis for %s finished (rc=0)", function_name)

	return completed.returncode


def main(argv: List[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="Generate clang analysis summaries for C functions/files.")
	parser.add_argument("--mode", choices=["file", "function"], required=True, help="运行模式：file 或 function")
	parser.add_argument("--file", dest="file_path", help="源文件路径（.c）")
	parser.add_argument("--function", dest="function_name", help="目标函数名（在 function 模式下必需）")
	parser.add_argument("--summary-dir", dest="summary_dir", required=True, help="clang summary 输出目录")
	parser.add_argument("--clang-bin", dest="clang_bin", default=DEFAULT_CLANG_BIN, help="clang 可执行文件路径")
	parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="不实际调用 clang，仅打印将要执行的命令")
	# Optional explicit project root: if provided, use this directory as cwd when running clang.
	# Otherwise the script will heuristically locate the project root (searching for .git, Makefile, etc.)
	parser.add_argument("--project-root", dest="project_root", help="可选：分析时使用的项目根目录（将切换到该目录运行 clang）")
	parser.add_argument("--compile-commands", dest="compile_commands", help="可选：compile_commands.json 路径，用于自动提取 -I/-D 标志以补充 clang 的预处理路径")

	args = parser.parse_args(argv)

	if args.mode == "function":
		if not args.file_path or not args.function_name:
			parser.error("--file 和 --function 在 function 模式下均为必需")

		rc = run_analysis_for_function(args.clang_bin, args.function_name, args.file_path, args.summary_dir, dry_run=args.dry_run, project_root=args.project_root, compile_commands=args.compile_commands)

		return rc

	# file 模式
	if not args.file_path:
		parser.error("--file 在 file 模式下必需")

	funcs = find_functions_in_c_file(args.file_path)
	if not funcs:
		logger.warning("在 %s 中未发现函数，退出。", args.file_path)
		return 0

	# 依次调用
	failed = []
	for fn in funcs:
		rc = run_analysis_for_function(args.clang_bin, fn, args.file_path, args.summary_dir, dry_run=args.dry_run, project_root=args.project_root, compile_commands=args.compile_commands)
		if rc != 0:
			failed.append((fn, rc))

	if failed:
		logger.error("%d functions failed: %s", len(failed), ", ".join(f"%s(rc=%d)" % t for t in failed))
		return 2

	logger.info("All analyses finished.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
