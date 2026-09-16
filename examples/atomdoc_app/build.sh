#!/bin/sh
# Bundle the widget module: viewer.js + atomdoc-ts (from an atomdoc checkout, with its zod),
# as one ES module. three.js stays external; the page's import map loads it from jsdelivr.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
ATOMDOC=${ATOMDOC:-$HOME/Dropbox/development/atomdoc}
mkdir -p "$HERE/build"
"$ATOMDOC/typescript/node_modules/.bin/esbuild" "$HERE/viewer.js" \
  --bundle --format=esm --target=es2022 --minify --legal-comments=none \
  --alias:atomdoc-ts="$ATOMDOC/typescript/src/index.ts" \
  --external:three --external:'three/addons/*' \
  --outfile="$HERE/build/viewer.bundle.js"
