#!/bin/sh
# sshd (keys only, forwarding allowed unless MACHINE_NO_FORWARDING=1) beside the image's own dockerd.
set -eu
ssh-keygen -A >/dev/null
forwarding=yes
[ "${MACHINE_NO_FORWARDING:-}" = "1" ] && forwarding=no
/usr/sbin/sshd -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
  -o PermitRootLogin=prohibit-password -o AllowTcpForwarding=$forwarding -o MaxStartups=100
# no TLS listener: the machine's Docker is used through its unix socket only, as on a real box
export DOCKER_TLS_CERTDIR=
exec dockerd-entrypoint.sh dockerd --host=unix:///var/run/docker.sock
