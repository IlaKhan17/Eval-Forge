#!/usr/bin/env bash
# EvalForge must stay application-neutral: Davis and AdaptQuiz are *reference
# integrations*, not product logic. Domain concepts leaking into the platform is
# the fastest way to corrupt it, and discipline alone does not prevent it.
#
# Reference integrations belong in examples/ and evals/ only.
set -euo pipefail

cd "$(dirname "$0")/.."

# Source only. Build output is excluded for the same reason bytecode is: it is a *copy* of source
# that lags behind it, so a match there reports a leak that may no longer exist — and .next in
# particular bundles every string in the app plus its dependencies, which made this fail on the word
# "davis" appearing inside a webpack chunk after someone ran `pnpm dev`. A generated artifact cannot
# leak a domain term that its source does not already contain.
if hits=$(grep -rniE \
  --exclude-dir=__pycache__ --exclude-dir=.next --exclude-dir=node_modules --exclude-dir=dist \
  --exclude='*.pyc' \
  '\b(davis|adaptquiz)\b' apps/ packages/ 2>/dev/null); then
  echo "✗ Application-specific terms found in platform code:"
  echo "$hits"
  echo
  echo "  Davis and AdaptQuiz are reference integrations. Move this to"
  echo "  examples/ or evals/, or generalize the concept."
  exit 1
fi

echo "✓ no application-specific terms in apps/ or packages/"
