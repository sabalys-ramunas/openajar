# A stand-in for a fresh Ubuntu 24.04 server, for tests/unit/test_environments_whole_docker.py and for
# trying `openajar environments machine add NAME root@host --whole` by hand: systemd as its first process,
# sshd, a root login by key, and the image's own user `ubuntu` with passwordless sudo (as a cloud image
# has). No Docker in it, so `machine add` without --whole can install Docker there for real.
# It needs --privileged and the host's cgroups (see the test's `box` fixture for the full `docker run`).
#
#   docker build -t openajar-test-server tests/fixtures/whole_machine
FROM ubuntu:24.04
RUN apt-get update -q \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
       systemd systemd-sysv dbus openssh-server sudo ca-certificates iproute2 \
    && apt-get clean && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh \
    && echo 'ubuntu ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-ubuntu && chmod 440 /etc/sudoers.d/90-ubuntu \
    && printf 'PasswordAuthentication no\nPermitRootLogin prohibit-password\nMaxStartups 100\n' \
       > /etc/ssh/sshd_config.d/01-test.conf \
    && systemctl enable ssh \
    && systemctl mask systemd-resolved.service systemd-networkd.service getty.target console-getty.service
STOPSIGNAL SIGRTMIN+3
EXPOSE 22
CMD ["/sbin/init"]
