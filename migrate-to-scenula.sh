#!/bin/bash
# Moves this Tessella install to Scenula. Scenula is installed from its own
# repository at the exact commit below, and its migrate command takes over the
# bar entry and the data. Later Scenula updates go through omarchy plugin update.
set -euo pipefail

url="https://github.com/goldbarth/omarchy-scenula"
# The reviewed Scenula release this migration runs. Nothing else is executed.
commit="c809fb6ef5de70acaccb80df1a92d2eda7d12822"
scenula="$HOME/.config/omarchy/plugins/goldbarth.scenula"
manual="Migrate manually instead: $url#upgrading-from-tessella"

fail() {
  echo "migrate-to-scenula: $*" >&2
  exit 1
}

cat <<EOF
Tessella is now Scenula. Same plugin, new name.

This will:
  1. install Scenula from $url
     at commit $commit
  2. move the bar widget and its settings from Tessella to Scenula
  3. keep your artists, images and history in place
  4. remove the Tessella plugin folder

shell.json is backed up first.

EOF
gum confirm "Migrate to Scenula now?" || exit 130

# A rerun after an interrupted migration finds Scenula already installed.
if [[ ! -e $scenula ]]; then
  # plugin add clones the default branch without running it; switch to the
  # pinned commit before anything from the checkout is executed.
  omarchy-plugin-add "$url" --yes
  git -C "$scenula" checkout --quiet --detach "$commit" ||
    fail "Scenula commit $commit was not found in $url"
fi

[[ -d $scenula/.git && ! -L $scenula ]] || fail "$scenula is not a plugin checkout. $manual"
[[ $(git -C "$scenula" rev-parse HEAD) == "$commit" ]] ||
  fail "$scenula is not at the expected Scenula commit $commit. $manual"
[[ -z $(git -C "$scenula" status --porcelain --ignored) ]] ||
  fail "$scenula contains changed or extra files. $manual"

echo
echo "Moving your Tessella setup to Scenula (no need to enable it separately)..."
python3 -I "$scenula/scenula.py" migrate
