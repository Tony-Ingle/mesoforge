#!/bin/sh
# Build the single MesoForge image from a clean commit.
#
# The build context is `git archive` of HEAD: exactly the committed bytes (never CRLF
# working-tree copies, .git, .env files, caches or runtime data), and the image records
# that commit as MESOFORGE_CODE_REVISION.  Usage: deploy/hosted/build-image.sh [TAG]
set -eu
cd "$(git rev-parse --show-toplevel)"
if [ -n "$(git status --porcelain)" ]; then
    echo "Refusing to build: commit or remove working-tree changes first" >&2
    exit 1
fi
revision=$(git rev-parse HEAD)
tag=${1:-mesoforge:$revision}
git -c core.autocrlf=false archive --format=tar "$revision" \
    | docker build --build-arg MESOFORGE_CODE_REVISION="$revision" --tag "$tag" -
echo "$tag"
