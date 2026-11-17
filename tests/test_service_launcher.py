import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import run_services
from run_services import identify, status, start_listener


class LauncherTests(unittest.TestCase):
    def process(self, root, args):
        process=Mock();process.cwd.return_value=str(root);process.cmdline.return_value=args
        return process

    def test_only_exact_project_listener_entrypoints_are_managed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for script,args,name in [('run_qq.py',[],'qq'),('run_douyin.py',['listen'],'douyin'),('run_wechat.py',[],'wechat'),('run_bilibili.py',['listen'],'bilibili')]:
                p=self.process(root,['python',str(root/script),*args])
                self.assertEqual(identify(p,root),name)
            for args in (['python','run_douyin.py','login'],['python','run_bilibili.py','check'],
                         ['python','-c','print("run_qq.py")'],['python',str(root.parent/'run_qq.py')]):
                self.assertIsNone(identify(self.process(root,args),root))

    def test_other_workspace_and_external_config_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            self.assertIsNone(identify(self.process(root.parent,['python','run_qq.py']),root))
            self.assertIsNone(identify(self.process(root,['python','run_wechat.py','--config','../other.json']),root))

    def test_reused_pid_does_not_inherit_old_ready_state(self):
        process=Mock();process.pid=123;process.create_time.return_value=200
        row=status('qq',dict(pid=123,created_at=100,ready=True),[process])
        self.assertTrue(row['running']);self.assertFalse(row['ready'])

    def test_stopped_process_is_not_reported_ready(self):
        row=status('qq',dict(pid=123,ready=True),[])
        self.assertFalse(row['ready']);self.assertFalse(row['running'])

    def test_api_and_legacy_douyin_configs_are_managed_without_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for config in ('douyin/config.api.local.json','douyin/config.json'):
                for flags in (['--config',config],['--config='+str(root/config)]):
                    with self.subTest(config=config,flags=flags):
                        self.assertEqual(identify(self.process(root,['python','run_douyin.py','listen',*flags]),root),'douyin')

    def test_external_equal_form_missing_and_duplicate_configs_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for flags in (['--config=../other.json'],['--config'],
                          ['--config','douyin/config.json','--config','douyin/config.api.local.json']):
                self.assertIsNone(identify(self.process(root,['python','run_douyin.py','listen',*flags]),root))

    def test_start_douyin_passes_api_config_without_starting_a_real_process(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_services,'REPORTS',Path(tmp)), \
                patch.object(run_services.subprocess,'Popen') as spawn, patch.object(run_services.psutil,'Process') as process:
            spawn.return_value.pid=123
            process.return_value.create_time.return_value=200
            row=start_listener('douyin')
            args=spawn.call_args.args[0]
            self.assertEqual(args[-4:],[str(run_services.ROOT/'run_douyin.py'),
                                       'listen','--config','douyin/config.api.local.json'])
            self.assertEqual(spawn.call_args.kwargs['cwd'],run_services.ROOT)
            self.assertFalse(row['ready'])

    def test_douyin_ready_markers_accept_api_and_browser_logs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(run_services,'REPORTS',Path(tmp)):
            log=Path(tmp)/'douyin.stdout.log'
            process=Mock();process.pid=123;process.create_time.return_value=200
            for marker in ('后台接口监听已连接','消息识别运行'):
                log.write_text(marker,encoding='utf-8')
                row=status('douyin',dict(pid=123,created_at=200,stdout=str(log)),[process])
                self.assertTrue(row['ready'])
            log.write_text('正在准备连接',encoding='utf-8')
            self.assertFalse(status('douyin',dict(pid=123,created_at=200,stdout=str(log)),[process])['ready'])


if __name__=='__main__':unittest.main()
