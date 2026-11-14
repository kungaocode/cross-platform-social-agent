"""从已确认的好友名单批量导入抖音白名单，不把会话列表冒充完整好友列表。"""
import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path


def import_friends(source, config_path, apply=False):
    raw=json.loads(Path(source).read_text(encoding='utf-8-sig'))
    if not isinstance(raw,list) or not all(isinstance(x,str) and x.strip() for x in raw):
        raise ValueError('好友文件应为联系人完整名称的 JSON 字符串数组')
    names=[x.strip() for x in raw]
    if not names or len(names)!=len(set(names)):
        raise ValueError('好友名单为空或含重名；请先人工确认，避免发送到错误会话')
    p=Path(config_path);config=json.loads(p.read_text(encoding='utf-8-sig'))
    added=[x for x in names if x not in config['peers']]
    if apply:
        shutil.copy2(p,p.with_name(p.name+'.'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.bak'))
        config['peers']=list(dict.fromkeys(config['peers']+names))
        # 导入不会开启监听，账号登录和启用必须分别完成。
        p.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return {'input_friends':len(names),'new_peers':len(added),'applied':apply,'enabled':config['enabled']}


if __name__=='__main__':
    root=Path(__file__).resolve().parent
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('friends',type=Path)
    parser.add_argument('--config',type=Path,default=root/'douyin/config.json')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    print(json.dumps(import_friends(args.friends,args.config,args.apply),ensure_ascii=False))
