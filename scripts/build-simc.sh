#!/usr/bin/env bash
# Fetch and build SimulationCraft into .work/, for running the pipeline locally.
# CI does the same thing inside .github/workflows/sims.yml.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$ROOT/.work"
SIMC="$WORK/simc"

mkdir -p "$WORK"

if [ -d "$SIMC/.git" ]; then
  echo "==> Updating SimulationCraft checkout"
  git -C "$SIMC" fetch --depth 1 origin
  git -C "$SIMC" reset --hard FETCH_HEAD
else
  echo "==> Cloning SimulationCraft"
  git clone --depth 1 https://github.com/simulationcraft/simc.git "$SIMC"
fi

echo "==> Configuring"
cmake -B "$SIMC/build" -S "$SIMC" \
  -DCMAKE_BUILD_TYPE=Release \
  -DSC_NO_NETWORKING=ON \
  -DBUILD_GUI=OFF

echo "==> Building (this takes a while the first time)"
cmake --build "$SIMC/build" -j"$(getconf _NPROCESSORS_ONLN)"

echo
echo "simc built at $SIMC/build/simc"
echo "Try:  wowdps list --profiles $SIMC/profiles"
