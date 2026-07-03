FROM alpine:3.20

RUN apk add --no-cache \
    iptables \
    iproute2 \
    iputils \
    bash \
    nmap-ncat \
    procps

# Install process_freezer script from faultstorm package
COPY faultstorm/scripts/process_freezer.sh /usr/local/bin/process_freezer.sh
RUN chmod +x /usr/local/bin/process_freezer.sh

# Keep container alive; test processes are started from behave steps
ENTRYPOINT ["tail", "-f", "/dev/null"]
