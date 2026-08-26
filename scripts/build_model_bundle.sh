#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build a prebuilt Lumena embedding-model bundle.
#
# Producers run this ONCE (with the export toolchain installed) to create a
# self-contained tarball that consumers/CI/Helm can download without ever
# installing optimum/torch. This is what makes the lean install work:
#
#   consumer install ──(no optimum)──> downloads this bundle ──> ready
#
# Output:
#   dist/bundles/<alias>.tar.gz            (the model dir: model.onnx + tokenizer)
#   dist/bundles/<alias>.tar.gz.sha256     (checksum for Helm model.sha256 / verify)
#
# Usage:
#   pip install 'lummenna[export]'          # the heavy toolchain, once, on the builder
#   ./scripts/build_model_bundle.sh                 # default: bge-small-en-v1.5
#   ./scripts/build_model_bundle.sh all-MiniLM-L6-v2
# ---------------------------------------------------------------------------
set -euo pipefail

ALIAS="${1:-bge-small-en-v1.5}"
OUT_DIR="${OUT_DIR:-dist/bundles}"
WORK_MODELS="$(mktemp -d)"

green="\033[0;32m"; blue="\033[0;34m"; red="\033[0;31m"; reset="\033[0m"
info(){ printf "${blue}==>${reset} %s\n" "$*"; }
ok(){ printf "${green}✓${reset}  %s\n" "$*"; }
die(){ printf "${red}✗ %s${reset}\n" "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "python3 not found"
python3 -c "import optimum.onnxruntime" 2>/dev/null \
  || die "Export toolchain missing. Install it on the builder:  pip install 'lummenna[export]'"

mkdir -p "$OUT_DIR"

info "Exporting '$ALIAS' to ONNX (this is the heavy step; builder-only)"
LUMENA_SOVEREIGN=false LUMENA_MODEL_PATH="$WORK_MODELS" \
  python3 -m lummenna.cli.main model download "$ALIAS" \
  || die "Model export failed"

MODEL_DIR="$WORK_MODELS/$ALIAS"
[ -f "$MODEL_DIR/model.onnx" ] || die "Expected $MODEL_DIR/model.onnx after export"

info "Packing bundle"
BUNDLE="$OUT_DIR/$ALIAS.tar.gz"
# Pack the CONTENTS of the model dir so it unpacks straight into
# <model_path>/<alias>/ (matching what the installer/Helm expect).
tar -C "$MODEL_DIR" -czf "$BUNDLE" .
sha256sum "$BUNDLE" | awk '{print $1}' > "$BUNDLE.sha256"

SIZE="$(du -h "$BUNDLE" | awk '{print $1}')"
SHA="$(cat "$BUNDLE.sha256")"
rm -rf "$WORK_MODELS"

ok "Bundle built: $BUNDLE ($SIZE)"
ok "SHA256: $SHA"
echo ""
echo "Consumers (lean install, no toolchain):"
echo "  LUMENA_PREBUILT_MODEL_URL=https://<host>/$ALIAS.tar.gz ./install/install.sh"
echo ""
echo "Helm (charts/lummenna/values.yaml):"
echo "  model:"
echo "    url: https://<host>/$ALIAS.tar.gz"
echo "    sha256: $SHA"
