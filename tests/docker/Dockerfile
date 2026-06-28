FROM alpine:3.20

RUN apk add --no-cache \
    iptables \
    iputils \
    bash \
    nmap-ncat \
    procps

# Keep container alive; test processes are started from behave steps
ENTRYPOINT ["tail", "-f", "/dev/null"]
