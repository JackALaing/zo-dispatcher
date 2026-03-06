#!/bin/bash
source /root/.zo_secrets
cd "$(dirname "$0")"
exec python -u -m zo_dispatcher.server
