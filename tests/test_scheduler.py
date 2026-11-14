import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from social_bot.scheduler import RequestPool, ScheduledResponse, SchedulerBusy


class SchedulerTests(unittest.TestCase):
    def test_independent_processes_share_concurrency_and_rate(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'pool.sqlite3'
            pool=RequestPool(path,concurrency=2,interval=.1)
            script='''import sys,time
from social_bot.scheduler import RequestPool
p=RequestPool(sys.argv[1],concurrency=2,interval=.1)
l=p.acquire()
with p.connect() as d:d.execute('INSERT INTO events VALUES(?,?)',('admitted',l.admitted_at))
with p.connect() as d:d.execute('INSERT INTO events VALUES(?,?)',('start',time.time()))
time.sleep(.3)
with p.connect() as d:d.execute('INSERT INTO events VALUES(?,?)',('end',time.time()))
l.close()
'''
            with pool.connect() as db:db.execute('CREATE TABLE events(kind TEXT,at REAL)')
            jobs=[subprocess.Popen([sys.executable,'-c',script,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(5)]
            for job in jobs:
                _,error=job.communicate(timeout=20)
                self.assertEqual(job.returncode,0,error.decode(errors='replace'))
            with pool.connect() as db:events=db.execute('SELECT kind,at FROM events ORDER BY at').fetchall()
            count=peak=0;starts=[]
            for kind,at in events:
                if kind=='admitted':
                    starts.append(at)
                    continue
                count+=1 if kind=='start' else -1;peak=max(peak,count)
            self.assertLessEqual(peak,2)
            self.assertEqual(count,0)
            self.assertTrue(all(b-a>=.08 for a,b in zip(starts,starts[1:])))

    def test_queue_timeout_and_response_close_release(self):
        with tempfile.TemporaryDirectory() as folder:
            pool=RequestPool(Path(folder)/'pool.sqlite3',concurrency=1,interval=0,wait_seconds=1)
            lease=pool.acquire()
            try:
                with self.assertRaises(SchedulerBusy):pool.acquire()
            finally:lease.close()
            with ScheduledResponse(io.BytesIO(b'test'),pool.acquire()) as response:
                self.assertEqual(response.read(),b'test')
            with pool.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM leases').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM waiting').fetchone()[0],0)

    def test_expired_crashed_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as folder:
            pool=RequestPool(Path(folder)/'pool.sqlite3',concurrency=1,interval=0)
            with pool.connect() as db:db.execute('INSERT INTO leases VALUES(?,?)',('crashed',time.time()-1))
            pool.acquire().close()
