#!/usr/bin/env bash
##
## `scripts/settings.sh`: Contains any optional, per-repo script settings/
## customizations.
##

settings::pre_release() {
  base::log_info "Updating version in pyproject.toml..."
  # Rewrites the line in place, keeping its quotes; `poetry version` would
  # requote it in its own style.
  local pattern="^version = (['\"])[^'\"]*(['\"])$"
  grep -Eq "${pattern}" pyproject.toml \
    || base::abort 'Failed to find the version line in pyproject.toml.'
  sed -E "s/${pattern}/version = \\1${_arg__new_version}\\2/" pyproject.toml > pyproject.toml.new
  mv pyproject.toml.new pyproject.toml
  git add pyproject.toml
}

settings::post_release() {
  base::log_info "Fast-forwarding the latest branch..."
  git fetch . HEAD:latest
  if ((_arg__push == 1)); then
    git push origin latest
  fi
}
