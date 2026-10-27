#!/bin/bash
# 一键部署（在服务器上以 root 执行；首次）
set -e
USER_NAME=trader
APP=/srv/trading
echo "① 建用户与目录"
id -u $USER_NAME >/dev/null 2>&1 || useradd -m -s /bin/bash $USER_NAME
mkdir -p $APP && chown -R $USER_NAME:$USER_NAME $APP
echo "② 系统准备（时区/防火墙/swap）"
timedatectl set-timezone Asia/Shanghai || true
[ -f /swapfile ] || (fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile)
ufw allow 22/tcp; ufw allow 80/tcp; ufw allow 443/tcp; yes | ufw enable || true
echo "③ 传代码（本机执行需改成 rsync 命令）"
echo "   rsync -av --exclude cache/pool_* --exclude .venv ./ $USER_NAME@SERVER:$APP/"
echo "④ 建 venv 装依赖"
sudo -u $USER_NAME bash -c "cd $APP && python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r requirements.txt"
echo "⑤ 安装 systemd 单元"
cp $APP/deploy/*.service $APP/deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now trading-board.service trading-daily.timer trading-lhb.timer
echo "⑥ 安装 Caddy（见 deploy/Caddyfile，需替换域名与密码哈希）"
echo "完成。检查：systemctl status trading-board / journalctl -u trading-board -f"
