ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}

ARG BUILD_VERSION
ARG BUILD_ARCH
ARG BUILD_DATE
ARG BUILD_DESCRIPTION
ARG BUILD_NAME
ARG BUILD_REF
ARG BUILD_REPOSITORY

COPY app/ /opt/home-mail/
WORKDIR /opt/home-mail
EXPOSE 8099

LABEL io.hass.type="addon" \
  io.hass.name="${BUILD_NAME}" \
  io.hass.description="${BUILD_DESCRIPTION}" \
  io.hass.arch="${BUILD_ARCH}" \
  io.hass.version="${BUILD_VERSION}" \
  org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
  org.opencontainers.image.revision="${BUILD_REF}" \
  org.opencontainers.image.created="${BUILD_DATE}"

CMD ["python", "-u", "/opt/home-mail/server.py"]
