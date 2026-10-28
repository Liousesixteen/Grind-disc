#!/usr/bin/env python3
"""Manage only the local 6063 research LaunchAgent. Never touches 6060."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
from urllib.request import urlopen

ROOT=Path(__file__).resolve().parents[1]
LABEL='com.liousesixteen.grind.research6063'
DEST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'
DOMAIN=f'gui/{os.getuid()}'


def definition():
    logs=ROOT/'cache/product';logs.mkdir(parents=True,exist_ok=True)
    return {'Label':LABEL,'ProgramArguments':[str(ROOT/'.venv/bin/python'),str(ROOT/'scripts/product_service.py'),'--port','6063'],
      'WorkingDirectory':str(ROOT),'EnvironmentVariables':{'TZ':'Asia/Shanghai','PYTHONUNBUFFERED':'1','TRADING_MANAGED':'1'},
      'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':10,'ProcessType':'Background',
      'StandardOutPath':str(logs/'launchd-bootstrap.log'),'StandardErrorPath':str(logs/'launchd-bootstrap-error.log')}


def status():
    p=subprocess.run(['launchctl','print',DOMAIN+'/'+LABEL],capture_output=True,text=True)
    result={'label':LABEL,'installed':DEST.is_file(),'loaded':p.returncode==0}
    if p.returncode==0:
        for key,pattern in [('pid',r'\bpid = (\d+)'),('last_exit_code',r'last exit code = (\d+)')]:
            m=re.search(pattern,p.stdout)
            if m:result[key]=int(m[1])
    try:
        with urlopen('http://127.0.0.1:6063/health',timeout=3) as r:result['health']=json.load(r)
    except Exception as e:result['health_error']=type(e).__name__
    return result


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('action',choices=('prepare','install','status','start','stop','restart','uninstall'));a=ap.parse_args()
    if sys.platform!='darwin':raise SystemExit('macOS控制工具；Linux使用deploy/grind-research.service')
    if a.action=='prepare':
        p=ROOT/'deploy'/f'{LABEL}.plist';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(plistlib.dumps(definition()));print(json.dumps({'prepared':str(p),'scope':'仅6063，本机回环地址，无账户/下单'}));return
    if a.action=='status':print(json.dumps(status(),ensure_ascii=False));return
    if a.action=='install':
        cfg=definition();DEST.parent.mkdir(parents=True,exist_ok=True)
        if DEST.exists():
            old=plistlib.loads(DEST.read_bytes())
            if old.get('Label')!=LABEL or str(ROOT/'scripts/product_service.py') not in old.get('ProgramArguments',[]):raise SystemExit('目标名称已有其他配置，拒绝覆盖')
            backup=ROOT/'cache/product/launchd-backups';backup.mkdir(parents=True,exist_ok=True);(backup/(dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'.plist')).write_bytes(DEST.read_bytes())
        temp=DEST.with_suffix('.tmp');temp.write_bytes(plistlib.dumps(cfg));temp.replace(DEST)
    if a.action=='restart' and status().get('loaded'):
        p=subprocess.run(['launchctl','kickstart','-k',DOMAIN+'/'+LABEL],capture_output=True,text=True)
        if p.returncode:raise SystemExit('LaunchAgent重新启动失败：'+str(p.returncode))
        print(json.dumps(status(),ensure_ascii=False));return
    if a.action in ('install','stop','uninstall'):
        subprocess.run(['launchctl','bootout',DOMAIN+'/'+LABEL],capture_output=True)
    if a.action in ('install','start','restart'):
        if not DEST.exists():raise SystemExit('请先install安装已准备的配置')
        p=subprocess.run(['launchctl','bootstrap',DOMAIN,str(DEST)],capture_output=True,text=True)
        if p.returncode and not status().get('loaded'):raise SystemExit('LaunchAgent加载失败：'+str(p.returncode)+'；当前桌面会话需允许用户级后台任务')
    if a.action=='uninstall' and DEST.exists():
        backup=ROOT/'cache/product/launchd-backups';backup.mkdir(parents=True,exist_ok=True);DEST.replace(backup/'uninstalled.plist')
    print(json.dumps(status(),ensure_ascii=False))

if __name__=='__main__':main()
