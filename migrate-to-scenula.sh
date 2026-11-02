#!/bin/bash
# Moves this Tessella install to Scenula. Scenula is installed from its own
# repository at the exact commit below, and its migrate command takes over the
# bar entry and the data. Later Scenula updates go through omarchy plugin update.
set -euo pipefail

url="https://github.com/goldbarth/omarchy-scenula"
# The reviewed Scenula release this migration runs. Nothing else is executed.
commit="c043376113b834fda5215862d69af4e81e20c0b9"
plugins="$HOME/.config/omarchy/plugins"
scenula="$plugins/goldbarth.scenula"
# Omarchy never discovers or watches hidden folders in the plugins directory.
stage="$plugins/.scenula-migrate.$$"
manual="Migrate manually instead: $url#upgrading-from-tessella"

fail() {
  echo "migrate-to-scenula: $*" >&2
  exit 1
}

cleanup() {
  rm -rf -- "$stage"
}
trap cleanup EXIT

cat <<EOF
Tessella is now Scenula. Same plugin, new name.

This will:
  1. install Scenula from $url
     at commit $commit
  2. move the bar widget and its settings from Tessella to Scenula
  3. keep your artists, images and history in place
  4. move the Tessella plugin folder aside (kept, hidden)

shell.json is backed up first.

EOF
gum confirm "Migrate to Scenula now?" || exit 130

# Build and verify the pinned tree while it is hidden. A --no-checkout clone
# contains no plugin files until the pinned commit is checked out.
mkdir -p "$plugins"
rm -rf -- "$stage"
GIT_TERMINAL_PROMPT=0 git clone --quiet --no-checkout -- "$url" "$stage" || fail "Could not clone $url"
git -C "$stage" checkout --quiet --detach "$commit" || fail "Scenula commit $commit was not found in $url"
[[ $(git -C "$stage" rev-parse HEAD) == "$commit" ]] || fail "Could not check out Scenula commit $commit"
omarchy-plugin-validate "$stage" >/dev/null || fail "Scenula commit $commit failed plugin validation"

if [[ -e $scenula || -L $scenula ]]; then
  # A rerun after an interrupted migration. Accept only the identical pinned
  # files; compared directly, so nothing from that folder runs before this.
  [[ -d $scenula && ! -L $scenula ]] || fail "$scenula is not a plugin folder. $manual"
  diff -r -q --no-dereference --exclude=.git -- "$stage" "$scenula" >/dev/null ||
    fail "$scenula differs from Scenula commit $commit. $manual"
else
  mv -T -- "$stage" "$scenula"
fi

echo
echo "Moving your Tessella setup to Scenula (no need to enable it separately)..."
python3 -I "$scenula/scenula.py" migrate
