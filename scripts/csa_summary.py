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


def build_clang_command(clang_bin: str, function_name: str, file_path: str, summary_dir: str) -> List[str]:
	"""构建 clang 分析命令的参数列表（适合传递给 subprocess.run）。

	说明：不再显式注入 -I 头文件路径；运行前会切换到项目根目录以便 clang 在相对路径下寻找头文件。
	"""

	cmd = [clang_bin, "--analyze"]
	# 保留 analyzer 的一些选项

	cmd += ["-Xanalyzer", "-analyzer-purge=none"]
	cmd += ["-Xanalyzer", "-analyzer-checker=alpha.core.DumpSummary"]
	cmd += ["-Xanalyzer", "-analyze-function"]
	cmd += ["-Xanalyzer", function_name, file_path]
	cmd += ["-Xanalyzer", "-analyzer-config", "-Xanalyzer", "clear-overlap-offset=false"]
	cmd += ["-Xanalyzer", "-analyzer-config", "-Xanalyzer", f"summary-dir={summary_dir}"]

	return cmd


def run_analysis_for_function(clang_bin: str, function_name: str, file_path: str, summary_dir: str, dry_run: bool = False, project_root: Optional[str] = None) -> int:
	"""对单个函数触发 clang 分析。返回子进程退出码（0 表示成功）。

	该函数会确保 summary_dir 存在。
	"""
	Path(summary_dir).mkdir(parents=True, exist_ok=True)

	# Determine project root: prefer explicit override, otherwise heuristically find it
	if project_root:
		proj_root = project_root
	else:
		proj_root = find_project_root(file_path)
	cmd = build_clang_command(clang_bin, function_name, file_path, summary_dir)

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

	args = parser.parse_args(argv)

	if args.mode == "function":
		if not args.file_path or not args.function_name:
			parser.error("--file 和 --function 在 function 模式下均为必需")

		rc = run_analysis_for_function(args.clang_bin, args.function_name, args.file_path, args.summary_dir, dry_run=args.dry_run, project_root=args.project_root)

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
		rc = run_analysis_for_function(args.clang_bin, fn, args.file_path, args.summary_dir, dry_run=args.dry_run, project_root=args.project_root)
		if rc != 0:
			failed.append((fn, rc))

	if failed:
		logger.error("%d functions failed: %s", len(failed), ", ".join(f"%s(rc=%d)" % t for t in failed))
		return 2

	logger.info("All analyses finished.")
	return 0


if __name__ == "__main__":
	sys.exit(main())
