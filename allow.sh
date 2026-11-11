#!/bin/bash
# Дать доступ к боту: bash /opt/french-bot/allow.sh 123456789 [987654321 ...]
set -e
ENV=/opt/french-bot/.env
for ID in "$@"; do
  [[ "$ID" =~ ^[0-9]+$ ]] || { echo "Не похоже на ID: $ID"; exit 1; }
  CUR=$(grep '^ALLOWED_IDS=' "$ENV" | cut -d= -f2)
  if [[ ",$CUR," == *",$ID,"* ]]; then
    echo "$ID уже в списке"
  else
    NEW=${CUR:+$CUR,}$ID
    sed -i "s/^ALLOWED_IDS=.*/ALLOWED_IDS=$NEW/" "$ENV"
    echo "Добавлен $ID"
  fi
done
systemctl restart french-bot
echo "Доступ: $(grep '^ALLOWED_IDS=' "$ENV" | cut -d= -f2)"
