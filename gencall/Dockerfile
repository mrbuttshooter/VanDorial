FROM python:3.12-slim

LABEL maintainer="GenCall Team"
LABEL description="GenCall - SIP Traffic Generator v2.0"

# Install SIPp
RUN apt-get update && apt-get install -y --no-install-recommends \
    sip-tester \
    libpcap-dev \
    libsctp-dev \
    && rm -rf /var/lib/apt/lists/*

# Set up GenCall
WORKDIR /opt/gencall

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir -e .

# Create directories
RUN mkdir -p /opt/gencall/logs /opt/gencall/media /opt/gencall/scenarios/custom

# Expose ports
EXPOSE 8080
EXPOSE 5060/udp
EXPOSE 5060/tcp
EXPOSE 10000-20000/udp

# Health check
HEALTHCHECK --interval=30s --timeout=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

ENV GENCALL_CONFIG=/opt/gencall/gencall/etc/gencall.cfg

ENTRYPOINT ["gencall-server"]
CMD ["--host", "0.0.0.0", "--port", "8080"]
