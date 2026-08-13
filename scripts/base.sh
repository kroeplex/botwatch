#!/usr/bin/env bash
##
## `scripts/base.sh`: Shared helpers sourced by the botwatch dev scripts.
##
## Provides banner/abort helpers and BASE__ROOT_DIR for the other scripts.
##

set -euo pipefail

BASE__ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BASE__COLOR_LIGHT_GREEN='\033[1;32m'
BASE__COLOR_OFF='\033[0m'

base::show_banner() {
  printf "\n===> ${BASE__COLOR_LIGHT_GREEN}%s${BASE__COLOR_OFF}\n" "$*" >&2
}

base::abort() {
  printf '[ERROR] %s\n' "${*:-Aborting.}" >&2
  exit 1
}
