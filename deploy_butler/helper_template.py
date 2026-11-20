"""Render the root helper and the one-line sudoers rule for a project.

The operator installs both files. Deploy Butler never does.
"""

from __future__ import annotations

from deploy_butler.db import Project
from deploy_butler.validate import RELEASE_RE, USER_RE, readiness_errors

_BASH = r"""#!/usr/bin/env bash
# Deploy Butler helper. Generated for one project.
# Install as root:root mode 0755. The app never installs this file.
# It does not print environment files or secrets.
set -euo pipefail
umask 077
export LC_ALL=C
unset TAR_OPTIONS

APP_DIR=__APP_DIR__
SERVICE_TYPE=__SERVICE_TYPE__
SERVICE_NAME=__SERVICE_NAME__
APP_OWNER=__APP_OWNER__
BACKUPS_DIR=__BACKUPS_DIR__
DEPLOY_MODE=__DEPLOY_MODE__
PROJECT=__PROJECT__
: "$PROJECT"
STAGING_DIR=__STAGING_DIR__
LOCK_FILE=__LOCK_FILE__
__DB_BLOCK__

TMPS=()
DIRS=()
cleanup() {
  local f
  if [[ ${#TMPS[@]} -gt 0 ]]; then
    for f in "${TMPS[@]}"; do
      rm -f -- "$f"
    done
  fi
  if [[ ${#DIRS[@]} -gt 0 ]]; then
    for f in "${DIRS[@]}"; do
      rm -rf -- "$f"
    done
  fi
}
trap cleanup EXIT

newtmp() {
  local f
  f="$(mktemp)"
  TMPS+=("$f")
  printf '%s\n' "$f"
}

die() {
  printf 'ERROR %s\n' "$*" >&2
  exit 1
}

need_release() {
  local rel="$1"
  if [[ ! "$rel" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    die "bad release name"
  fi
}

guard_dir() {
  local dir="$1"
  local part
  local depth=0
  local IFS='/'
  local -a parts=()
  case "$dir" in
    /*) ;;
    *) die "path must be absolute" ;;
  esac
  read -r -a parts <<< "${dir#/}"
  for part in "${parts[@]}"; do
    case "$part" in
      ""|.|..) die "bad path component" ;;
    esac
    depth=$((depth + 1))
  done
  if [[ "$depth" -lt 2 ]]; then
    die "path too shallow"
  fi
  case "$dir" in
    /bin|/boot|/dev|/etc|/lib|/lib64|/proc|/root|/run|/sbin|/sys|/usr|/var|/opt|/home|/tmp|/srv)
      die "refusing protected path"
      ;;
  esac
}

path_ok() {
  local p="$1"
  local part
  local IFS='/'
  local -a parts=()
  case "$p" in
    ""|/*|*\\*) return 1 ;;
  esac
  read -r -a parts <<< "$p"
  if [[ ${#parts[@]} -eq 0 ]]; then
    return 1
  fi
  for part in "${parts[@]}"; do
    case "$part" in
      ""|.|..) return 1 ;;
    esac
  done
  return 0
}

take_lock() {
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    printf '%s\n' "ERROR busy" >&2
    exit 2
  fi
}

sha_of() {
  local sum
  sum="$(sha256sum -- "$1")"
  printf '%s\n' "${sum%% *}"
}

cmd_hashes() {
  local line full sum trimmed
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" ]] && continue
    trimmed="${line%/}"
    if ! path_ok "$trimmed"; then
      die "bad path"
    fi
    full="${APP_DIR}/${trimmed}"
    if [[ -L "$full" ]]; then
      printf 'SYMLINK\t%s\n' "$trimmed"
    elif [[ -f "$full" ]]; then
      sum="$(sha_of "$full")"
      printf '%s\t%s\n' "$sum" "$trimmed"
    else
      printf 'MISSING\t%s\n' "$trimmed"
    fi
  done
  printf '%s\n' "OK"
}

backup_databases() {
  local rel="$1"
  local p bname dbdest dbsize
  if [[ ${#DB_PATHS[@]} -eq 0 ]]; then
    return 0
  fi
  for p in "${DB_PATHS[@]}"; do
    [[ -z "$p" ]] && continue
    if [[ -L "$p" || ! -f "$p" ]]; then
      die "db missing"
    fi
    bname="$(basename -- "$p")"
    dbdest="${BACKUPS_DIR}/${rel}__${bname}"
    sqlite3 "$p" ".timeout 5000" ".backup '${dbdest}'"
    dbsize="$(stat -c%s -- "$dbdest")"
    printf 'DB %s %s\n' "$dbsize" "$dbdest"
  done
}

restore_databases() {
  local rel="$1"
  local p bname src
  if [[ ${#DB_PATHS[@]} -eq 0 ]]; then
    return 0
  fi
  for p in "${DB_PATHS[@]}"; do
    [[ -z "$p" ]] && continue
    bname="$(basename -- "$p")"
    src="${BACKUPS_DIR}/${rel}__${bname}"
    if [[ ! -f "$src" || -L "$src" ]]; then
      die "db backup missing"
    fi
    sqlite3 "$p" ".timeout 5000" ".restore '${src}'"
  done
}

cmd_backup() {
  local rel="$1"
  local flag="${2:-}"
  local with_db=0
  local dest parent base size target
  need_release "$rel"
  guard_dir "$APP_DIR"
  guard_dir "$BACKUPS_DIR"
  if [[ "$flag" == "--db" ]]; then
    with_db=1
  elif [[ -n "$flag" ]]; then
    die "unknown backup flag"
  fi
  mkdir -p -- "$BACKUPS_DIR"
  dest="${BACKUPS_DIR}/${rel}.tar.gz"
  parent="$(dirname -- "$APP_DIR")"
  base="$(basename -- "$APP_DIR")"
  if [[ "$DEPLOY_MODE" == "releases" ]]; then
    if [[ -L "$APP_DIR" ]]; then
      target="$(readlink -f -- "$APP_DIR")"
      tar -C "$(dirname -- "$target")" -czf "$dest" "$(basename -- "$target")"
      readlink -f -- "$APP_DIR" > "${BACKUPS_DIR}/${rel}.previous"
    elif [[ -e "$APP_DIR" ]]; then
      die "releases mode requires app_dir to be a symlink"
    else
      tar -czf "$dest" --files-from /dev/null
      printf '%s\n' "NONE" > "${BACKUPS_DIR}/${rel}.previous"
    fi
  else
    if [[ -L "$APP_DIR" ]]; then
      die "in-place mode requires a real directory"
    elif [[ -d "$APP_DIR" ]]; then
      tar -C "$parent" -czf "$dest" "$base"
    elif [[ ! -e "$APP_DIR" ]]; then
      tar -czf "$dest" --files-from /dev/null
    else
      die "app_dir is not a directory"
    fi
  fi
  size="$(stat -c%s -- "$dest")"
  printf 'APP %s\n' "$size"
  if [[ "$with_db" -eq 1 ]]; then
    backup_databases "$rel"
  fi
  printf '%s\n' "OK"
}

ARCHIVE_LIST=""

reject_archive_listing() {
  local archive="$1"
  local list line ftype trimmed
  list="$(newtmp)"
  if ! tar -tvzf "$archive" > "$list"; then
    die "cannot list archive"
  fi
  while IFS= read -r line; do
    if [[ -z "$line" ]]; then
      continue
    fi
    ftype="${line:0:1}"
    case "$ftype" in
      -|d) ;;
      *) die "link or special file in archive" ;;
    esac
    case "$line" in
      *" link to "*) die "hardlink in archive" ;;
    esac
  done < "$list"
  if ! tar -tzf "$archive" > "$list"; then
    die "cannot list archive"
  fi
  while IFS= read -r line; do
    if [[ -z "$line" ]]; then
      continue
    fi
    trimmed="${line%/}"
    if ! path_ok "$trimmed"; then
      die "bad path in archive"
    fi
  done < "$list"
  ARCHIVE_LIST="$list"
}

print_applied_hashes() {
  local root="$1"
  local list="$2"
  local line trimmed sum full
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    trimmed="${line%/}"
    full="${root}/${trimmed}"
    if [[ -L "$full" ]]; then
      die "symlink after extract"
    fi
    if [[ -f "$full" ]]; then
      sum="$(sha_of "$full")"
      printf '%s\t%s\n' "$sum" "$trimmed"
    fi
  done < "$list"
}

cmd_apply() {
  local rel="$1"
  local archive="$2"
  local real stage backup list dest relroot work copy
  need_release "$rel"
  guard_dir "$APP_DIR"
  guard_dir "$BACKUPS_DIR"
  case "$archive" in
    "$STAGING_DIR"/*.tar.gz) ;;
    *) die "archive outside staging" ;;
  esac
  if [[ -L "$archive" || ! -f "$archive" ]]; then
    die "archive missing"
  fi
  real="$(readlink -f -- "$archive")"
  stage="$(readlink -f -- "$STAGING_DIR")"
  case "$real" in
    "$stage"/*.tar.gz) ;;
    *) die "archive outside staging" ;;
  esac
  work="$(mktemp -d)"
  chmod 700 -- "$work"
  DIRS+=("$work")
  copy="${work}/archive.tar.gz"
  cp -- "$real" "$copy"
  chmod 600 -- "$copy"
  backup="${BACKUPS_DIR}/${rel}.tar.gz"
  if [[ ! -f "$backup" || -L "$backup" ]]; then
    die "backup missing"
  fi
  reject_archive_listing "$copy"
  list="$ARCHIVE_LIST"
  if [[ "$DEPLOY_MODE" == "releases" ]]; then
    relroot="$(dirname -- "$APP_DIR")/releases"
    dest="${relroot}/${rel}"
    if [[ -e "$dest" || -L "$dest" ]]; then
      die "release exists"
    fi
    mkdir -p -- "$dest"
    tar --no-same-owner --no-overwrite-dir --no-same-permissions -xzf "$copy" -C "$dest"
    if [[ -n "$APP_OWNER" ]]; then
      chown -R -- "$APP_OWNER" "$dest"
    fi
    if [[ -L "$APP_DIR" ]]; then
      readlink -f -- "$APP_DIR" > "${BACKUPS_DIR}/${rel}.previous"
    elif [[ ! -e "$APP_DIR" ]]; then
      mkdir -p -- "$(dirname -- "$APP_DIR")"
      printf '%s\n' "NONE" > "${BACKUPS_DIR}/${rel}.previous"
    else
      die "app_dir must be a symlink in releases mode"
    fi
    ln -sfn -- "$dest" "$APP_DIR"
    print_applied_hashes "$dest" "$list"
  else
    if [[ -L "$APP_DIR" ]]; then
      die "app_dir must be a directory"
    fi
    mkdir -p -- "$APP_DIR"
    tar --no-same-owner --no-overwrite-dir --no-same-permissions -xzf "$copy" -C "$APP_DIR"
    if [[ -n "$APP_OWNER" ]]; then
      chown -R -- "$APP_OWNER" "$APP_DIR"
    fi
    print_applied_hashes "$APP_DIR" "$list"
  fi
  printf '%s\n' "OK"
}

service_is_active() {
  local st pid
  case "$SERVICE_TYPE" in
    none)
      printf '%s\n' "active"
      ;;
    systemd)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      st="$(systemctl is-active -- "$SERVICE_NAME" || true)"
      printf '%s\n' "$st"
      if [[ "$st" != "active" ]]; then
        return 1
      fi
      ;;
    pm2)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      pid="$(pm2 pid "$SERVICE_NAME" 2>/dev/null | tr -d '[:space:]' || true)"
      if [[ "$pid" =~ ^[0-9]+$ ]] && [[ "$pid" -gt 0 ]]; then
        printf '%s\n' "active"
      else
        printf '%s\n' "inactive"
        return 1
      fi
      ;;
    *)
      die "bad service type"
      ;;
  esac
}

cmd_restart() {
  case "$SERVICE_TYPE" in
    none)
      printf '%s\n' "active"
      ;;
    systemd)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      systemctl restart -- "$SERVICE_NAME"
      service_is_active
      ;;
    pm2)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      pm2 restart "$SERVICE_NAME"
      service_is_active
      ;;
    *)
      die "bad service type"
      ;;
  esac
}

cmd_status() {
  service_is_active
}

cmd_logs() {
  local seconds="$1"
  if [[ ! "$seconds" =~ ^[0-9]+$ ]]; then
    die "bad seconds"
  fi
  case "$SERVICE_TYPE" in
    none)
      ;;
    systemd)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      journalctl -u "$SERVICE_NAME" --since "-${seconds} seconds ago" --no-pager -o cat
      ;;
    pm2)
      [[ -n "$SERVICE_NAME" ]] || die "no service name"
      pm2 logs "$SERVICE_NAME" --nostream --lines 200 --raw
      ;;
    *)
      die "bad service type"
      ;;
  esac
  printf '%s\n' "OK"
}

cmd_rollback() {
  local rel="$1"
  local flag="${2:-}"
  local with_db=0
  local backup parent prevf prev relroot
  need_release "$rel"
  guard_dir "$APP_DIR"
  guard_dir "$BACKUPS_DIR"
  if [[ "$flag" == "--db" ]]; then
    with_db=1
  elif [[ -n "$flag" ]]; then
    die "unknown rollback flag"
  fi
  backup="${BACKUPS_DIR}/${rel}.tar.gz"
  if [[ ! -f "$backup" || -L "$backup" ]]; then
    die "backup missing"
  fi
  if [[ "$DEPLOY_MODE" == "releases" ]]; then
    prevf="${BACKUPS_DIR}/${rel}.previous"
    if [[ ! -f "$prevf" || -L "$prevf" ]]; then
      die "previous missing"
    fi
    prev="$(tr -d '[:space:]' < "$prevf")"
    relroot="$(dirname -- "$APP_DIR")/releases"
    if [[ "$prev" == "NONE" ]]; then
      rm -f -- "$APP_DIR"
    else
      case "$prev" in
        "$relroot"/*) ;;
        *) die "previous outside releases" ;;
      esac
      if [[ ! -d "$prev" || -L "$prev" ]]; then
        die "previous release missing"
      fi
      ln -sfn -- "$prev" "$APP_DIR"
    fi
  else
    if [[ -L "$APP_DIR" ]]; then
      die "refusing to remove a symlink app_dir"
    fi
    parent="$(dirname -- "$APP_DIR")"
    rm -rf -- "$APP_DIR"
    mkdir -p -- "$parent"
    tar --no-same-owner -xzf "$backup" -C "$parent"
    if [[ ! -d "$APP_DIR" ]]; then
      mkdir -p -- "$APP_DIR"
    fi
    if [[ -n "$APP_OWNER" && -d "$APP_DIR" ]]; then
      chown -R -- "$APP_OWNER" "$APP_DIR"
    fi
  fi
  if [[ "$with_db" -eq 1 ]]; then
    restore_databases "$rel"
  fi
  printf '%s\n' "OK"
}

cmd_list() {
  local f size
  mkdir -p -- "$BACKUPS_DIR"
  shopt -s nullglob
  for f in "$BACKUPS_DIR"/*.tar.gz; do
    if [[ -f "$f" && ! -L "$f" ]]; then
      size="$(stat -c%s -- "$f")"
      printf '%s %s\n' "$size" "$(basename -- "$f")"
    fi
  done
  shopt -u nullglob
  printf '%s\n' "OK"
}

cmd_prune() {
  local keep="$1"
  local list count drop i line path base
  local relroot current total remain
  if [[ ! "$keep" =~ ^[0-9]+$ ]]; then
    die "bad keep"
  fi
  guard_dir "$BACKUPS_DIR"
  mkdir -p -- "$BACKUPS_DIR"
  list="$(newtmp)"
  find "$BACKUPS_DIR" -maxdepth 1 -type f -name '*.tar.gz' -printf '%T@ %p\n' | sort -n > "$list"
  count="$(wc -l < "$list" | tr -d '[:space:]')"
  drop=0
  if [[ "$count" -gt "$keep" ]]; then
    drop=$((count - keep))
  fi
  i=0
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    if [[ "$i" -lt "$drop" ]]; then
      path="${line#* }"
      base="$(basename -- "$path" .tar.gz)"
      if [[ "$base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
        rm -f -- "$path"
        rm -f -- "${BACKUPS_DIR}/${base}.previous"
        shopt -s nullglob
        local -a matches=( "${BACKUPS_DIR}/${base}__"* )
        if [[ ${#matches[@]} -gt 0 ]]; then
          rm -f -- "${matches[@]}"
        fi
        shopt -u nullglob
      fi
    fi
    i=$((i + 1))
  done < "$list"
  if [[ "$DEPLOY_MODE" == "releases" ]]; then
    relroot="$(dirname -- "$APP_DIR")/releases"
    current=""
    if [[ -L "$APP_DIR" ]]; then
      current="$(readlink -f -- "$APP_DIR" || true)"
    fi
    if [[ -d "$relroot" ]]; then
      list="$(newtmp)"
      find "$relroot" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -n > "$list"
      total="$(wc -l < "$list" | tr -d '[:space:]')"
      remain="$total"
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        if [[ "$remain" -le "$keep" ]]; then
          break
        fi
        path="${line#* }"
        if [[ -n "$current" && "$path" == "$current" ]]; then
          continue
        fi
        base="$(basename -- "$path")"
        case "$base" in
          [A-Za-z0-9]*) ;;
          *) continue ;;
        esac
        if [[ "$base" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
          case "$path" in
            "$relroot"/*)
              if [[ -d "$path" && ! -L "$path" ]]; then
                rm -rf -- "$path"
                remain=$((remain - 1))
              fi
              ;;
          esac
        fi
      done < "$list"
    fi
  fi
  printf '%s\n' "OK"
}

main() {
  local cmd="${1:-}"
  if [[ $# -gt 0 ]]; then
    shift
  fi
  take_lock
  case "$cmd" in
    hashes) cmd_hashes ;;
    backup) cmd_backup "$@" ;;
    apply) cmd_apply "$@" ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    rollback) cmd_rollback "$@" ;;
    list-backups) cmd_list ;;
    prune) cmd_prune "$@" ;;
    *) die "unknown command" ;;
  esac
}

main "$@"
"""


def _sq(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("value cannot be embedded in the helper")
    return "'" + value + "'"


def _db_block(paths: list[str]) -> str:
    if not paths:
        return "DB_PATHS=()"
    lines = "\n".join(_sq(path) for path in paths)
    return "DB_PATHS=(\n" + lines + "\n)"


def assert_renderable(project: Project) -> None:
    errors = readiness_errors(project.public_dict())
    if errors:
        raise ValueError("project is not ready to render a helper")
    if not USER_RE.fullmatch(project.ssh_user):
        raise ValueError("ssh user is not safe for sudoers")
    if not RELEASE_RE.fullmatch(project.name):
        raise ValueError("project name is not safe")


def render_helper(project: Project) -> str:
    assert_renderable(project)
    replacements = {
        "__APP_DIR__": _sq(project.app_dir),
        "__SERVICE_TYPE__": _sq(project.service_type),
        "__SERVICE_NAME__": _sq(project.service_name),
        "__APP_OWNER__": _sq(project.app_owner),
        "__BACKUPS_DIR__": _sq(project.backups_dir),
        "__DEPLOY_MODE__": _sq(project.deploy_mode),
        "__PROJECT__": _sq(project.name),
        "__STAGING_DIR__": _sq(project.staging_dir),
        "__LOCK_FILE__": _sq(f"/run/lock/deploy-butler-{project.name}.lock"),
        "__DB_BLOCK__": _db_block(project.db_backup_paths),
    }
    script = _BASH
    for key, value in replacements.items():
        if key not in script:
            raise ValueError(f"missing placeholder {key}")
        script = script.replace(key, value)
    if "__" in script.split("main()", 1)[0] and "__APP" in script:
        raise ValueError("unreplaced placeholder")
    return script


def render_sudoers(project: Project) -> str:
    assert_renderable(project)
    path = project.helper_path
    user = project.ssh_user
    return (
        f"# Deploy Butler sudoers for {project.name}\n"
        "# Only this helper. No wildcards and no other commands.\n"
        f"{user} ALL=(root) NOPASSWD: {path}\n"
    )


def install_steps(project: Project) -> list[dict[str, str]]:
    """Command lines the operator runs. The app does not run them."""
    script = project.helper_path
    sudoers = project.sudoers_path
    user = project.ssh_user
    return [
        {
            "id": "sqlite3",
            "command": "command -v sqlite3 || sudo apt-get install -y sqlite3",
        },
        {
            "id": "copy_helper",
            "command": f"sudo install -o root -g root -m 0755 deploy-butler-{project.name} {script}",
        },
        {
            "id": "copy_sudoers",
            "command": f"sudo install -o root -g root -m 0440 deploy-butler-{project.name}.sudoers {sudoers}",
        },
        {
            "id": "visudo",
            "command": f"sudo visudo -cf {sudoers}",
        },
        {
            "id": "whole",
            "command": "sudo visudo -c",
        },
        {
            "id": "check_sudo",
            "command": f"sudo -n -l -U {user}",
        },
        {
            "id": "smoke",
            "command": f"sudo -n {script} list-backups",
        },
        {
            "id": "remove_sudoers",
            "command": f"sudo rm -f {sudoers}",
        },
        {
            "id": "remove_helper",
            "command": f"sudo rm -f {script}",
        },
        {
            "id": "recheck",
            "command": "sudo visudo -c",
        },
    ]
