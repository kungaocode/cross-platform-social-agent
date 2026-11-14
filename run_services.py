"""Start, restart, stop or inspect this project's four chat listeners."""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import psutil
from social_bot.network_policy import direct_environment

ROOT = Path(__file__).resolve().parent
REPORTS = ROOT / 'reports'
STATE = REPORTS / 'services-on-e.json'
COMMANDS = {'wechat':['run_wechat.py'], 'qq':['run_qq.py'],
            'bilibili':['run_bilibili.py','listen'], 'douyin':['run_douyin.py','listen']}
LABELS = {'wechat':'微信','qq':'QQ','bilibili':'B站','douyin':'抖音'}
READY = {'wechat':'微信适配器已启动','qq':'QQ 已连接',
         'bilibili':'bilibili_listener_ready','douyin':'消息识别运行'}


def identify(process, root=ROOT):
    """Match an exact entrypoint and working directory, never just a PID/name."""
    try:
        if Path(process.cwd()).resolve() != root.resolve(): return None
        args = process.cmdline()
        if not args or not Path(args[0]).stem.lower().startswith('python'): return None
        for name, command in COMMANDS.items():
            for i, arg in enumerate(args[1:], 1):
                if Path(arg).name != command[0]: continue
                if (root/arg).resolve() != (root/command[0]).resolve(): continue
                rest = args[i+1:]
                if name in ('douyin','bilibili') and 'listen' not in rest: continue
                if '--config' in rest:
                    config = Path(rest[rest.index('--config')+1])
                    expected = root/('config.json' if name=='wechat' else name+'/config.json')
                    if (root/config).resolve() != expected.resolve(): continue
                return name
    except (psutil.Error,OSError,ValueError,IndexError): pass
    return None


def discover():
    result = {name:[] for name in COMMANDS}
    for process in psutil.process_iter():
        name = identify(process)
        if name: result[name].append(process)
    return result


@contextmanager
def manager_lock():
    REPORTS.mkdir(exist_ok=True)
    with (REPORTS/'services-manager.lock').open('a+b') as handle:
        handle.seek(0)
        if not handle.read(1):handle.write(b'0');handle.flush()
        handle.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RuntimeError('另一个启动程序正在操作，请稍后再试') from None
        try:yield
        finally:
            handle.seek(0)
            if os.name=='nt':msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)


def read_state():
    if not STATE.exists():return {}
    records=json.loads(STATE.read_text(encoding='utf-8-sig'))
    return {row['platform']:row for row in records}


def save_state(records):
    temp=STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(list(records.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    temp.replace(STATE)


def terminate_listener(process):
    if not identify(process):raise RuntimeError('进程身份已变化，拒绝停止')
    children=process.children(recursive=True)
    # The desktop WeChat/QQ clients are not launched or terminated by this manager.
    owned=[process,*children]
    for item in owned:
        try:item.terminate()
        except psutil.NoSuchProcess:pass
    _,alive=psutil.wait_procs(owned,timeout=8)
    for item in alive:
        try:item.kill()
        except psutil.NoSuchProcess:pass
    _,alive=psutil.wait_procs(alive,timeout=3)
    if alive:raise RuntimeError('旧服务仍未停止，未创建重复服务')


def start_listener(name):
    stamp=datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    out=REPORTS/f'{name}-chat-{stamp}.stdout.log'
    err=REPORTS/f'{name}-chat-{stamp}.stderr.log'
    options={}
    if os.name=='nt':
        startup=subprocess.STARTUPINFO();startup.dwFlags|=subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow=0
        options.update(startupinfo=startup,creationflags=subprocess.CREATE_NO_WINDOW)
    else:options['start_new_session']=True
    with out.open('wb') as stdout,err.open('wb') as stderr:
        process=subprocess.Popen([sys.executable,'-X','utf8','-u',str(ROOT/COMMANDS[name][0]),*COMMANDS[name][1:]],
            cwd=ROOT,env=dict(direct_environment(),PYTHONIOENCODING='utf-8'),stdin=subprocess.DEVNULL,
            stdout=stdout,stderr=stderr,**options)
    return dict(platform=name,pid=process.pid,created_at=psutil.Process(process.pid).create_time(),
                stdout=str(out),stderr=str(err),network='direct',memory_drive='E:',ready=False)


def status(name, record, processes):
    result={**record,'platform':name,'running':bool(processes),'duplicate_count':max(0,len(processes)-1)}
    if not processes:result.update(ready=False,status='已停止');return result
    pid=processes[0].pid
    same=record.get('pid')==pid and (not record.get('created_at') or abs(record['created_at']-processes[0].create_time())<.01)
    result['pid']=pid
    text=''
    if same:
        for key in ('stdout','stderr'):
            path=Path(record.get(key,''))
            if path.is_file() and path.resolve().is_relative_to(REPORTS.resolve()):
                with path.open('rb') as f:
                    f.seek(max(0,path.stat().st_size-65536));text+=f.read().decode('utf-8',errors='replace')
    ready=same and (READY[name] in text or bool(record.get('ready')))
    result.update(ready=bool(ready),status='已就绪' if ready else '运行中，等待就绪')
    if name=='douyin' and ('captcha_container' in text or '要求人工安全验证' in text):
        result.update(ready=False,status='需在专用浏览器完成人工验证')
    return result


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['start','restart','stop','status'],nargs='?',default='start')
    parser.add_argument('--only',choices=list(COMMANDS),nargs='+')
    parser.add_argument('--wait',type=int,default=45,help='等待就绪的最长秒数，最多60秒')
    args=parser.parse_args()
    selected=args.only or list(COMMANDS)
    with manager_lock():
        records=read_state();live=discover()
        if args.action in ('stop','restart'):
            for name in selected:
                for process in live[name]:terminate_listener(process)
                records.pop(name,None)
                print(LABELS[name]+'：已停止',flush=True)
            save_state(records);live=discover()
        if args.action in ('start','restart'):
            for name in selected:
                if live[name]:
                    print(LABELS[name]+'：已在运行，不重复启动',flush=True)
                else:
                    records[name]=start_listener(name)
                    save_state(records)
                    print(LABELS[name]+f'：启动中 PID={records[name]["pid"]}',flush=True)
            deadline=time.monotonic()+max(0,min(args.wait,60))
            while time.monotonic()<deadline:
                live=discover()
                statuses=[status(name,records.get(name,{}),live[name]) for name in selected]
                if all(row['ready'] or not row['running'] for row in statuses):break
                time.sleep(.5)
        live=discover()
        statuses=[status(name,records.get(name,{}),live[name]) for name in selected]
        for row in statuses:
            if row['running']:
                records[row['platform']]=row
            print(f"{LABELS[row['platform']]}：{row['status']}"+f"  PID={row.get('pid','-')}",flush=True)
            if not row['running'] and args.action!='stop':
                print('  请检查登录/配置；日志：'+str(row.get('stderr','未记录')),flush=True)
        save_state(records)
        (REPORTS/'services-status.json').write_text(json.dumps(statuses,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return 0 if args.action=='stop' or all(row['ready'] for row in statuses) else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except (RuntimeError,OSError,ValueError) as exc:
        print('启动管理失败：'+str(exc),file=sys.stderr);raise SystemExit(1)
