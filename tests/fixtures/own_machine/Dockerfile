# A stand-in for a person's own machine, for tests/unit/test_environments_own_docker.py and for trying
# `openajar environments machine add` by hand: a Linux box with sshd and a Docker of its own (Docker's
# official docker-in-docker image, which needs --privileged). The test gives it a login key
# (/root/.ssh/authorized_keys) and reaches it on a loopback port of this computer.
#
#   docker build -t openajar-test-machine tests/fixtures/own_machine
FROM docker:27-dind
# --upgrade: the base image pins an older openssh client than the server package of the day wants
RUN apk add --no-cache --upgrade openssh \
    && sed -i 's/^root:!/root:*/' /etc/shadow \
    && mkdir -p /root/.ssh && chmod 700 /root/.ssh
COPY machine-entry.sh /usr/local/bin/machine-entry.sh
RUN chmod 755 /usr/local/bin/machine-entry.sh
EXPOSE 22
ENTRYPOINT ["/usr/local/bin/machine-entry.sh"]
