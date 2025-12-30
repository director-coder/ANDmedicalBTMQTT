FROM python:3.12-alpine

RUN apk add --no-cache dbus dbus-libs bluez

# dbus-next — лёгкая библиотека для работы с DBus
RUN pip install --no-cache-dir dbus-next

COPY rootfs/ /
RUN chmod +x /usr/bin/run.sh

CMD ["/usr/bin/run.sh"]
