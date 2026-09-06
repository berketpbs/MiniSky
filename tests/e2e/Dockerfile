# A throwaway stand-in for a cloud VM: a container running real sshd, so the
# half of MiniSky that only exists over SSH can be tested for free.
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server rsync python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /var/run/sshd /root/.ssh && chmod 700 /root/.ssh

# Root login with a key only - no passwords.
RUN sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config \
    && sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && ssh-keygen -A

# authorized_keys is written at container start by tests/e2e/conftest.py,
# from whatever key SSHKeyManager would hand the real launch path - baking
# a key into the image would make it specific to one developer.

EXPOSE 22
CMD ["/usr/sbin/sshd", "-D", "-e"]
