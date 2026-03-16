FROM ubuntu:22.04

RUN apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        iptables sudo curl python3 net-tools iproute2

# Create users
RUN useradd -m appuser && \
    useradd -m proxyuser && \
    useradd -m otheruser

# Allow all users to run curl via sudo without password (for test harness)
RUN echo "ALL ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

WORKDIR /workspace
