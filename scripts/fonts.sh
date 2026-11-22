#!/usr/bin/env bash
set -euo pipefail
mkdir -p fonts
base=https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004
for weight in Regular Bold; do
  curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
    "$base/Sans/OTF/Korean/NotoSansCJKkr-$weight.otf" -o "fonts/NotoSansCJKkr-$weight.otf"
done
sha256sum --check <<'HASHES'
6bcb2a0703aa137e874fc2dffa85f6c21ba9a67fa329e81b8c801663af7e992a  fonts/NotoSansCJKkr-Regular.otf
26d0c6748500a0444844280b308f5b62c7ae92ac6c6ac88148e502dd211eb52a  fonts/NotoSansCJKkr-Bold.otf
HASHES
curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 \
  "$base/LICENSE" -o fonts/OFL.txt
