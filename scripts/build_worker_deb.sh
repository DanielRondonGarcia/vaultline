#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s VERSION ARCH FROZEN_BINARY [OUTPUT_DIR]\n' "$0" >&2
  printf '  VERSION: bare semver such as 1.2.3\n' >&2
  printf '  ARCH: amd64 or arm64\n' >&2
  printf '  VAULTLINE_RESTIC_BINARY / VAULTLINE_RCLONE_BINARY (env): verified Restic and rclone\n' >&2
  printf '    executables, installed to /usr/lib/vaultline beside the worker\n' >&2
}

if [[ $# -lt 3 || $# -gt 4 ]]; then
  usage
  exit 2
fi

VERSION="$1"
ARCH="$2"
FROZEN_BINARY="$3"
OUTPUT_DIR="${4:-dist}"

if ! [[ ${VERSION} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  printf 'error: VERSION must be a bare semver tag like 1.2.3\n' >&2
  exit 2
fi

case "${ARCH}" in
  amd64|arm64)
    ;;
  *)
    printf 'error: ARCH must be amd64 or arm64\n' >&2
    exit 2
    ;;
esac

if [[ ! -f "${FROZEN_BINARY}" || ! -x "${FROZEN_BINARY}" ]]; then
  printf 'error: FROZEN_BINARY must be an existing executable file\n' >&2
  exit 2
fi

# The frozen worker resolves Restic and rclone from /usr/lib/vaultline before
# the copies PyInstaller extracts to a temporary directory, so the package must
# ship the same verified executables the spec bundled.
RESTIC_BINARY="${VAULTLINE_RESTIC_BINARY:-}"
RCLONE_BINARY="${VAULTLINE_RCLONE_BINARY:-}"
if [[ -z "${RESTIC_BINARY}" || ! -f "${RESTIC_BINARY}" || ! -x "${RESTIC_BINARY}" ]]; then
  printf 'error: VAULTLINE_RESTIC_BINARY must point to the verified Restic executable\n' >&2
  exit 2
fi
if [[ -z "${RCLONE_BINARY}" || ! -f "${RCLONE_BINARY}" || ! -x "${RCLONE_BINARY}" ]]; then
  printf 'error: VAULTLINE_RCLONE_BINARY must point to the verified rclone executable\n' >&2
  exit 2
fi

if ! command -v dpkg-deb >/dev/null 2>&1; then
  printf 'error: dpkg-deb is required on the build host\n' >&2
  exit 127
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEMPLATE_ROOT="${REPO_ROOT}/packaging/debian"
RESTIC_LICENSE="${REPO_ROOT}/packaging/licenses/restic-v0.19.1-LICENSE"
RCLONE_LICENSE="${REPO_ROOT}/packaging/licenses/rclone-v1.75.2-COPYING"
PACKAGE_NAME="vaultline-worker"
ASSET_NAME="vaultline-worker_${VERSION}_${ARCH}.deb"
OUTPUT_PATH="${OUTPUT_DIR%/}/${ASSET_NAME}"
WORKDIR="$(mktemp -d)"
PACKAGE_ROOT="${WORKDIR}/${PACKAGE_NAME}"

cleanup() {
  rm -rf "${WORKDIR}"
}
trap cleanup EXIT

mkdir -p "${OUTPUT_DIR}"
if [[ -e "${OUTPUT_PATH}" ]]; then
  printf 'error: output already exists: %s\n' "${OUTPUT_PATH}" >&2
  exit 2
fi

install -d -m 0755 "${PACKAGE_ROOT}/DEBIAN"
install -d -m 0755 "${PACKAGE_ROOT}/usr/bin"
install -d -m 0755 "${PACKAGE_ROOT}/usr/lib/vaultline"
install -d -m 0755 "${PACKAGE_ROOT}/lib/systemd/system"
install -d -m 0755 "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker"

if [[ ! -f "${RESTIC_LICENSE}" ]]; then
  printf 'error: bundled Restic license is missing: %s\n' "${RESTIC_LICENSE}" >&2
  exit 2
fi
if [[ ! -f "${RCLONE_LICENSE}" ]]; then
  printf 'error: bundled rclone license is missing: %s\n' "${RCLONE_LICENSE}" >&2
  exit 2
fi

sed \
  -e "s/@VERSION@/${VERSION}/g" \
  -e "s/@ARCH@/${ARCH}/g" \
  "${TEMPLATE_ROOT}/control.in" > "${PACKAGE_ROOT}/DEBIAN/control"
chmod 0644 "${PACKAGE_ROOT}/DEBIAN/control"

install -m 0755 "${TEMPLATE_ROOT}/postinst" "${PACKAGE_ROOT}/DEBIAN/postinst"
install -m 0755 "${TEMPLATE_ROOT}/prerm" "${PACKAGE_ROOT}/DEBIAN/prerm"
install -m 0755 "${FROZEN_BINARY}" "${PACKAGE_ROOT}/usr/bin/vaultline-worker"
install -m 0755 "${RESTIC_BINARY}" "${PACKAGE_ROOT}/usr/lib/vaultline/restic"
install -m 0755 "${RCLONE_BINARY}" "${PACKAGE_ROOT}/usr/lib/vaultline/rclone"
install -m 0644 "${TEMPLATE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service" \
  "${PACKAGE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service"
install -m 0644 "${TEMPLATE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example" \
  "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example"
install -m 0644 "${RESTIC_LICENSE}" \
  "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/restic-v0.19.1-LICENSE"
install -m 0644 "${RCLONE_LICENSE}" \
  "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/rclone-v1.75.2-COPYING"

test -f "${PACKAGE_ROOT}/DEBIAN/control"
test -x "${PACKAGE_ROOT}/DEBIAN/postinst"
test -x "${PACKAGE_ROOT}/DEBIAN/prerm"
test -x "${PACKAGE_ROOT}/usr/bin/vaultline-worker"
test -x "${PACKAGE_ROOT}/usr/lib/vaultline/restic"
test -x "${PACKAGE_ROOT}/usr/lib/vaultline/rclone"
test -f "${PACKAGE_ROOT}/lib/systemd/system/docker-volume-backup-worker.service"
test -f "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/worker.env.example"
test -f "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/restic-v0.19.1-LICENSE"
test -f "${PACKAGE_ROOT}/usr/share/doc/vaultline-worker/rclone-v1.75.2-COPYING"

LC_ALL=C dpkg-deb --build --root-owner-group "${PACKAGE_ROOT}" "${OUTPUT_PATH}"

test -f "${OUTPUT_PATH}"
printf '%s\n' "${OUTPUT_PATH}"
