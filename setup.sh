#!/usr/bin/env bash
# Build the local speech engine. Run once after cloning.
# Downloads ~190 MB (whisper.cpp source + base model). Nothing leaves your machine after this.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d whisper.cpp ]; then
  git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git
fi

cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_SERVER=OFF
cmake --build build -j"$(nproc)" --config Release

if [ ! -f models/ggml-base.bin ]; then
  bash ./models/download-ggml-model.sh base
fi

echo
echo "Ready.  Run the app with:  python3 gui/ui.py"
