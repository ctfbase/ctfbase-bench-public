FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/root

# Core utilities
RUN apt-get update && apt-get install -y \
    curl wget git unzip tar build-essential \
    python3 python3-pip python3-venv \
    gdb nmap netcat-openbsd socat \
    binwalk foremost exiftool \
    tshark john hashcat \
    radare2 sqlmap \
    iptables ipset dnsutils \
    jq file xxd \
    && rm -rf /var/lib/apt/lists/*

# Python CTF tools
RUN pip3 install --break-system-packages \
    pwntools pycryptodome requests z3-solver gmpy2 beautifulsoup4

# Install OpenCode
RUN curl -fsSL https://opencode.ai/install | bash
ENV PATH="/root/.opencode/bin:$PATH"

# Guarantee no global configs exist
RUN rm -rf /root/.config/opencode /root/.local/share/opencode || true

# Copy scripts
COPY entrypoint.sh /entrypoint.sh
COPY apply-network-whitelist.sh /apply-network-whitelist.sh
RUN chmod +x /entrypoint.sh /apply-network-whitelist.sh

ENTRYPOINT ["/entrypoint.sh"]
WORKDIR /workspace
