#!/usr/bin/env bash
set -euo pipefail
task_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
: "${MAXLAB_ROOT:?Set MAXLAB_ROOT to the official maxlab_lib directory}"
cmake -S "$task_root/cpp" -B "$task_root/cpp/build" -DMAXLAB_ROOT="$MAXLAB_ROOT"
cmake --build "$task_root/cpp/build"
