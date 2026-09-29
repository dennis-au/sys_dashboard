FROM alpine:3.22

RUN apk add --no-cache curl jq

COPY --chmod=0555 bootstrap.sh /bootstrap/bootstrap.sh

USER 1000:1000
