# Переезд на свой сервер (VPS): мгновенные шабашки

На сервере бот проверяет каналы **раз в минуту**, а не раз в 10, и не зависит от правил GitHub. Хватит самого дешёвого VPS: 1 ядро и 1 ГБ RAM. Сам бот занимает около 60–100 МБ памяти.

**Где брать:** сервер **не в России** (Нидерланды, Германия, Финляндия): в РФ Telegram замедляют. Подойдёт любой провайдер с оплатой российской картой или через СБП. Система: Ubuntu 24.04.

## Установка (команды по очереди, в SSH)
```bash
# 1. отдельный пользователь без прав администратора
sudo adduser --disabled-password --gecos "" shab
sudo apt update && sudo apt install -y python3-venv git
sudo -iu shab

# 2. код (тот же репозиторий, что на GitHub)
git clone https://github.com/<твой-логин>/shabashki.git shabashki-bot && cd shabashki-bot
python3 -m venv .venv
.venv/bin/pip install --no-deps --require-hashes -r requirements-build.txt
.venv/bin/pip install --no-deps --require-hashes --no-build-isolation -r requirements.txt

# 3. настройки (те же значения, что в GitHub Secrets)
cat > .env <<'CFG'
BOT_TOKEN=...
STATE_KEY=...
# OWNER_ID=...    (если задавал в GitHub)
STATE_PATH=/home/shab/shabashki-bot/state.bin
# нейросеть (необязательно): GitHub токен (fine-grained) с правом Models: read
# LLM_TOKEN=github_pat_...
# только для закрытых чатов через отдельный аккаунт:
# TG_API_ID=...
# TG_API_HASH=...
CFG
chmod 600 .env

# 4. забрать готовое состояние из GitHub (вход повторять не нужно)
git fetch origin state && git show FETCH_HEAD:state.bin > state.bin
exit
```

## ⚠️ Сначала выключи GitHub-расписание
Две копии с одной сессией одновременно делать нельзя: Telegram завершит сессию. На GitHub зайди в Actions → «Шабашки: проверка» → `…` → **Disable workflow**.

## Запуск службы
```bash
sudo cp /home/shab/shabashki-bot/deploy/vps/shabashki.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now shabashki
sudo systemctl status shabashki      # должно быть active (running)
journalctl -u shabashki -f           # логи (только счётчики)
```

Если вход нужен заново: `sudo -iu shab`, потом `cd shabashki-bot && set -a && . ./.env && set +a && .venv/bin/python -m shabashki login`. QR придёт в бота, как и раньше.
