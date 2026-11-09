#!/usr/bin/env bash
# One-time provisioning. Run only after reviewing this new standalone repository.
set -Eeuo pipefail
umask 077

cd "$(dirname "${BASH_SOURCE[0]}")"
REPO='Pitriss/debian-repo'
SIGNING_SECRET='APT_REPO_GPG_PRIVATE_KEY'
KEY_HOME="${HOME}/.local/share/debian-repo/gnupg"

for cmd in gh git gpg python3 dpkg; do
  command -v "$cmd" >/dev/null 2>&1 || { printf 'ERROR: command not found: %s\n' "$cmd" >&2; exit 1; }
done

gh auth status >/dev/null || { printf 'ERROR: please authenticate with gh auth login first\n' >&2; exit 1; }
current_user="$(gh api user --jq .login)"
if [[ "${current_user,,}" != 'pitriss' ]]; then
  printf 'ERROR: authenticated GitHub user must be Pitriss (got %s)\n' "$current_user" >&2
  exit 1
fi

# Keep all Git operations within this new standalone project.
if ! git rev-parse --show-toplevel >/dev/null 2>&1; then
  git init --initial-branch=main
fi
if [[ "$(git rev-parse --show-toplevel)" != "$(pwd -P)" ]]; then
  printf 'ERROR: current directory is not the root of its own Git repository\n' >&2
  exit 1
fi
if [[ "$(git branch --show-current)" != 'main' ]]; then
  printf 'ERROR: expected main branch\n' >&2
  exit 1
fi
# Set the public noreply identity only in this new repository.
account_id="$(gh api user --jq .id)"
git config --local user.name "$current_user"
git config --local user.email "${account_id}+${current_user}@users.noreply.github.com"

# Do not create or change anything in kencoder/pfx-split-qt/reddit2tg/Jira-2-Teams.
git add .gitignore .github config scripts tests README.md publish-github.sh
if ! git diff --cached --quiet; then
  git commit -m 'feat: bootstrap signed Debian APT repository'
fi

if gh repo view "$REPO" --json name --jq .name >/dev/null 2>&1; then
  printf 'Remote GitHub repository already exists: %s\n' "$REPO"
else
  printf 'Creating public GitHub repository: %s\n' "$REPO"
  gh repo create "$REPO" --public --source=. --remote=origin --description 'Signed Debian APT repository for approved Pitriss releases'
fi
if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "https://github.com/${REPO}.git"
fi
actual_remote="$(git remote get-url origin)"
case "$actual_remote" in
  "git@github.com:${REPO}.git"|"https://github.com/${REPO}.git"|"ssh://git@github.com/${REPO}.git") ;;
  *) printf 'ERROR: origin points to unexpected remote: %s\n' "$actual_remote" >&2; exit 1 ;;
esac

git push --set-upstream origin main

if gh api "repos/${REPO}/pages" --jq .build_type >/dev/null 2>&1; then
  gh api --method PUT "repos/${REPO}/pages" -f build_type=workflow >/dev/null
else
  gh api --method POST "repos/${REPO}/pages" -f build_type=workflow >/dev/null
fi

# A stable long-lived signing key. This home is NOT part of Git.
mkdir -p "$KEY_HOME"
chmod 700 "$KEY_HOME"
if ! gpg --homedir "$KEY_HOME" --batch --with-colons --list-secret-keys | grep -q '^sec:'; then
  printf 'Generating RSA 3072 signing key in %s\n' "$KEY_HOME"
  gpg --homedir "$KEY_HOME" --batch --pinentry-mode loopback --passphrase '' \
      --quick-generate-key 'Pitriss Debian APT Repository' rsa3072 sign 3y
fi
secret_count="$(gpg --homedir "$KEY_HOME" --batch --with-colons --list-secret-keys | grep -c '^sec:' || true)"
if [[ "$secret_count" -ne 1 ]]; then
  printf 'ERROR: expected exactly one signing key (found %s). Manual inspection needed.\n' "$secret_count" >&2
  exit 1
fi
fingerprint="$(gpg --homedir "$KEY_HOME" --batch --with-colons --list-secret-keys | awk -F: '$1=="fpr" { print $10; exit }')"
if [[ -z "$fingerprint" ]]; then
  printf 'ERROR: missing signing fingerprint\n' >&2
  exit 1
fi
# Never echo or save the private key in this repository or in a log.
gpg --homedir "$KEY_HOME" --batch --armor --export-secret-keys "$fingerprint" | \
    gh secret set "$SIGNING_SECRET" --repo "$REPO"

printf 'SIGNING_KEY_FINGERPRINT=%s\n' "$fingerprint"
printf 'Signing key stays locally in %s; back it up securely (do not commit it).\n' "$KEY_HOME"
printf 'Setting up first workflow run...\n'

dispatched=0
for attempt in 1 2 3 4 5; do
  if gh workflow run publish.yml --repo "$REPO" --ref main; then
    dispatched=1
    break
  fi
  sleep 3
done
if [[ "$dispatched" -ne 1 ]]; then
  printf 'ERROR: workflow dispatch failed. After checking GitHub Actions run: gh workflow run publish.yml -R %s\n' "$REPO" >&2
  exit 1
fi
printf 'GitHub Actions: https://github.com/%s/actions\n' "$REPO"
printf 'APT URL after deployment: https://pitriss.github.io/debian-repo/\n'
