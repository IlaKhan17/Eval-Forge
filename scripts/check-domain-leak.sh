#!/usr/bin/env bash
# EvalForge must stay application-neutral: Davis and AdaptQuiz are *reference
# integrations*, not product logic. Domain concepts leaking into the platform is
# the fastest way to corrupt it, and discipline alone does not prevent it.
#
# Reference integrations belong in examples/ and evals/ only.
set -euo pipefail

cd "$(dirname "$0")/.."

# --exclude-dir keeps compiled bytecode out of it: a stale .pyc reports a match
# that no longer exists in the source, which is a confusing way to fail a build.
if hits=$(grep -rniE --exclude-dir=__pycache__ --exclude='*.pyc' \
  '\b(davis|adaptquiz)\b' apps/ packages/ 2>/dev/null); then
  echo "✗ Application-specific terms found in platform code:"
  echo "$hits"
  echo
  echo "  Davis and AdaptQuiz are reference integrations. Move this to"
  echo "  examples/ or evals/, or generalize the concept."
  exit 1
fi

echo "✓ no application-specific terms in apps/ or packages/"
