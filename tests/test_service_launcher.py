import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from run_services import identify, status


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


if __name__=='__main__':unittest.main()
