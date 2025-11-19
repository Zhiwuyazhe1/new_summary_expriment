#!/usr/bin/env python3
"""
clang_analyze.py

跨平台脚本：从指定的 compile_commands.json 中为目标源文件提取 include / 宏参数，
并把这些参数补充给 clang 的 --analyze 调用。

Usage (PowerShell):
  python .\scripts\clang_analyze.py --clang-bin C:\path\to\clang.exe \
    --src-file crypto/mem.c --compile-commands compile_commands.json \
    --summary-dir /path/to/summary --function CRYPTO_mem_leaks_fp

该脚本会：
 - 在 compile_commands.json 中按 file 精确匹配或按后缀（endswith）查找条目
 - 解析 command 或 arguments 字段，提取 -I/-isystem/-iquote/-D/-std 等相关标志
 - 拼接并调用 clang --analyze，保留原脚本中的 analyzer 配置

"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def load_compile_commands(path: Path) -> List[dict]:
    with path.open('r', encoding='utf-8') as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit(f"Error: {path} does not contain a JSON array of compile commands")
    return data


def find_entry(entries: List[dict], src_file: str) -> Optional[dict]:
    # try exact match first
    for e in entries:
        if 'file' in e and Path(e['file']).as_posix() == Path(src_file).as_posix():
            return e
    # try endswith match
    for e in entries:
        if 'file' in e and Path(e['file']).name == Path(src_file).name:
            return e
    # try substring match
    for e in entries:
        if 'file' in e and Path(src_file).as_posix().endswith(Path(e['file']).name):
            return e
    return None


def tokenize_command(entry: dict) -> List[str]:
    # compile_commands entries may have 'arguments' (array) or 'command' (string)
    if 'arguments' in entry and isinstance(entry['arguments'], list):
        # ensure strings
        return [str(x) for x in entry['arguments']]
    if 'command' in entry and isinstance(entry['command'], str):
        # use shlex to split, posix True usually works; on Windows commands may use different quoting
        try:
            return shlex.split(entry['command'], posix=True)
        except Exception:
            # fallback: naive split
            return entry['command'].split()
    raise SystemExit('Error: compile_commands entry has no command/arguments')


def extract_relevant_flags(tokens: List[str], src_file: str) -> List[str]:
    keep: List[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        # skip compiler binary (first token) and -c, -o <file>
        if i == 0 and (Path(t).name.lower().startswith('clang') or Path(t).name.lower().startswith('gcc') or Path(t).name.lower().endswith('.exe')):
            i += 1
            continue
        if t == '-c':
            i += 1
            continue
        if t == '-o':
            i += 2
            continue

        # if token matches the source file, skip it
        if Path(t).as_posix() == Path(src_file).as_posix() or Path(t).name == Path(src_file).name:
            i += 1
            continue

        # include flags
        if t.startswith('-I') or t.startswith('-isystem') or t.startswith('-iquote'):
            keep.append(t)
            i += 1
            continue

        # -I with separated arg
        if t in ('-I', '-isystem', '-iquote'):
            if i + 1 < len(tokens):
                keep.append(t)
                keep.append(tokens[i + 1])
                i += 2
                continue

        # macros and language flags often needed
        if t.startswith('-D') or t.startswith('-std=') or t.startswith('-std'):
            keep.append(t)
            i += 1
            continue

        # keep other flags that commonly affect preprocessing
        if t in ('-idirafter', '-iprefix'):
            if i + 1 < len(tokens):
                keep.append(t)
                keep.append(tokens[i + 1])
                i += 2
                continue

        i += 1
    return keep


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description='Run clang --analyze using flags extracted from compile_commands.json')
    p.add_argument('--clang-bin', required=True, help='Path to clang binary (e.g. /usr/bin/clang-15 or C:\\\\path\\\\clang.exe)')
    p.add_argument('--src-file', required=True, help='Source file path (e.g. crypto/mem.c)')
    p.add_argument('--compile-commands', required=True, help='Path to compile_commands.json')
    p.add_argument('--summary-dir', required=True, help='Directory for analyzer summary output')
    p.add_argument('--function', dest='analyze_function', default=None, help='Optional function name to analyze (passed to -Xanalyzer -analyze-function)')
    p.add_argument('--extra-flags', nargs='*', help='Any extra flags to pass to clang', default=[])
    args = p.parse_args(argv)

    clang_bin = args.clang_bin
    src_file = args.src_file
    cc_path = Path(args.compile_commands)
    summary_dir = args.summary_dir

    if not cc_path.exists():
        print(f"Error: compile_commands.json not found at {cc_path}")
        return 2

    entries = load_compile_commands(cc_path)
    entry = find_entry(entries, src_file)
    if entry is None:
        print(f"Error: 未能在 {cc_path} 中找到与 {src_file} 匹配的条目。请确保路径匹配或使用相对/绝对路径。")
        return 3

    tokens = tokenize_command(entry)
    flags = extract_relevant_flags(tokens, src_file)

    # Add any extra user-provided flags
    flags.extend(args.extra_flags)

    print('>>> Found compile command tokens:')
    print(' '.join(tokens))
    print('\n>>> Extracted flags to pass to clang:')
    print(' '.join(flags))
    print(f">>> Analysis summary dir: {summary_dir}")

    # Build clang analyzer command
    clang_cmd = [clang_bin, '--analyze'] + flags + [
        '-Xanalyzer', '-analyzer-purge=none',
        '-Xanalyzer', '-analyzer-checker=alpha.core.DumpSummary'
    ]

    if args.analyze_function:
        clang_cmd += ['-Xanalyzer', '-analyze-function', '-Xanalyzer', args.analyze_function]

    clang_cmd += [
        '-Xanalyzer', '-analyzer-config', '-Xanalyzer', 'clear-overlap-offset=false',
        '-Xanalyzer', '-analyzer-config', '-Xanalyzer', f'summary-dir={summary_dir}'
    ]

    # Finally the source file
    clang_cmd.append(src_file)

    print('\n>>> Running clang command:')
    print(' '.join(clang_cmd))

    try:
        ret = subprocess.call(clang_cmd)
        return ret
    except FileNotFoundError:
        print(f"Error: clang binary not found at '{clang_bin}'")
        return 4
    except Exception as e:
        print('Error: Failed to run clang:', e)
        return 5


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
