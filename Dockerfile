# Runs as root on purpose (SonarQube docker:S6471, accepted as safe by the maintainer, #2810): /workdir/results and /workdir/tests are
# meant to be bind-mounted, and a non-root user would fail to write to a host folder it does not own.
FROM alpine@sha256:f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf

RUN apk update && \
    apk --no-cache add tzdata ca-certificates && \
    rm -rf /var/cache/apk/*

COPY LICENSE /usr/share/licenses/venom/LICENSE
COPY NOTICE /usr/share/licenses/venom/NOTICE
COPY dist/venom.linux-amd64 /usr/local/bin/venom
RUN chmod +x /usr/local/bin/venom

VOLUME /workdir/results
VOLUME /workdir/tests
WORKDIR /workdir

ENV VENOM_OUTPUT_DIR=/workdir/results \
    VENOM_LIB_DIR=/workdir/tests/lib \
    VENOM_VERBOSE=1

ENTRYPOINT ["/usr/local/bin/venom"]
CMD ["run", "./tests/*.y*ml"]