#!/bin/sh
set -eu
if [ -f /etc/nginx/certs/tls.crt ] && [ -f /etc/nginx/certs/tls.key ]; then
  cp /etc/nginx/tls.conf.template /etc/nginx/conf.d/tls.conf
else
  rm -f /etc/nginx/conf.d/tls.conf
fi
exec nginx -g 'daemon off;'
