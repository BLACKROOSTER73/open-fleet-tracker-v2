#!/bin/bash
# Full path (inside the project): open-fleet-tracker/pelican/install.sh
#
# Paste this into the egg's "Install Script" field in the Pelican panel.
# It runs in a separate, throwaway, root-privileged container -- NOT the
# container that actually runs the tracker -- every time you create the
# server, or click "Reinstall" on an existing server. That's the mechanism
# that pulls updated files from GitHub.
#
# Recommended "Script Container" for this script: ghcr.io/parkervcp/installers:debian
# Recommended "Script Entry": bash
#
# Expects one egg Variable:
#   GIT_REPOSITORY  -- defaults to this project's own repo,
#                      https://github.com/BLACKROOSTER73/open-fleet-tracker-v2.git,
#                      if left blank/unset. Point it at your own fork/repo
#                      instead if you want to track your own copy.
#                      (for a private repo, use a token in the URL instead:
#                      https://<TOKEN>@github.com/<you>/<repo>.git)
# Optional egg Variable:
#   GIT_BRANCH      -- defaults to "main" if left blank/unset.

apt-get update >/dev/null 2>&1
apt-get install -y git ca-certificates >/dev/null 2>&1

# The install container runs as root, but /mnt/server is owned by a
# different UID (Pelican's "container" user). Git refuses to operate on a
# directory it doesn't consider "safe" in that situation and prints:
#   fatal: detected dubious ownership in repository at '/mnt/server'
# Explicitly trust it so clone/fetch/reset below actually run.
git config --global --add safe.directory /mnt/server

cd /mnt/server || exit 1

REPO="${GIT_REPOSITORY:-https://github.com/BLACKROOSTER73/open-fleet-tracker-v2.git}"
BRANCH="${GIT_BRANCH:-main}"

if [ -d .git ]; then
    echo "Existing repo found -- fetching latest from ${BRANCH}..."
    git fetch origin
    git reset --hard "origin/${BRANCH}"
else
    echo "No repo found yet -- cloning ${REPO} (${BRANCH})..."
    git clone --branch "${BRANCH}" "${REPO}" .
fi

# config.ini is gitignored, so a fetch/reset above never touches it once it
# exists. On a brand new install there won't be one yet, so seed it from the
# secret-free template.
if [ ! -f config.ini ] && [ -f example.config.ini ]; then
    echo "No config.ini found -- creating one from example.config.ini."
    cp example.config.ini config.ini
    echo "IMPORTANT: edit config.ini over SFTP and fill in your real secrets before starting the server."
fi

echo "Install complete."
