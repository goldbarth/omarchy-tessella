#!/bin/bash
# Moves this Tessella install to Scenula. Scenula is installed from its own
# repository and its migrate command takes over the bar entry and the data.
set -euo pipefail

url="https://github.com/goldbarth/omarchy-scenula"
scenula="$HOME/.config/omarchy/plugins/goldbarth.scenula"

cat <<EOF
Tessella is now Scenula. Same plugin, new name.

This will:
  1. install Scenula from $url
  2. move the bar widget and its settings from Tessella to Scenula
  3. keep your artists, images and history in place
  4. remove the Tessella plugin folder

shell.json is backed up first.

EOF
gum confirm "Migrate to Scenula now?" || exit 130

# A rerun after an interrupted migration finds Scenula already installed.
if [[ ! -d $scenula ]]; then
  omarchy-plugin-add "$url" --yes
fi
echo
echo "Moving your Tessella setup to Scenula (no need to enable it separately)..."
python3 -I "$scenula/scenula.py" migrate
