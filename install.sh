#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
UNIT_TEMPLATE="$PROJECT_DIR/deploy/turzx-monitor.service.in"
UDEV_RULE="$PROJECT_DIR/deploy/99-turzx-monitor.rules"
UNIT_TARGET="/etc/systemd/system/turzx-monitor.service"
UDEV_TARGET="/etc/udev/rules.d/99-turzx-monitor.rules"
RAPL_ENERGY="/sys/class/powercap/intel-rapl:0/energy_uj"

if [[ ${EUID} -ne 0 ]]; then
    exec sudo -- "$0" "$@"
fi

SERVICE_USER="${SERVICE_USER:-$(stat -c '%U' "$PROJECT_DIR")}"

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    echo "usuário do serviço não existe: $SERVICE_USER" >&2
    exit 1
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
    echo "ambiente virtual ausente: $PROJECT_DIR/.venv" >&2
    echo "crie com: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [[ ! -f "$PROJECT_DIR/config.yaml" ]]; then
    echo "configuração ausente: $PROJECT_DIR/config.yaml" >&2
    exit 1
fi

temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT

sed \
    -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    "$UNIT_TEMPLATE" > "$temporary"

install -m 0644 "$temporary" "$UNIT_TARGET"
install -m 0644 "$UDEV_RULE" "$UDEV_TARGET"

if getent group dialout >/dev/null; then
    usermod -aG dialout "$SERVICE_USER"
fi

udevadm control --reload-rules
udevadm trigger --subsystem-match=tty --subsystem-match=powercap

if [[ -e "$RAPL_ENERGY" ]]; then
    chmod a+r "$RAPL_ENERGY"
fi

systemctl daemon-reload
systemctl enable turzx-monitor.service
systemctl restart turzx-monitor.service

sleep 3
systemctl --no-pager --full status turzx-monitor.service || true
