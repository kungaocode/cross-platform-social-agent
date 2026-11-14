"""Separate two-hour follow-up of this run's published comment roots only."""
import time


class RunThreads:
    def __init__(self, registry, roots):
        self.registry,self.roots=registry,{str(root) for root in roots}

    def owns(self, platform, account, root, **kwargs):
        return str(root) in self.roots and self.registry.owns(platform,account,root,**kwargs)

    def get(self, platform, account, root):
        return self.registry.get(platform,account,root) if str(root) in self.roots else None


def watch_comments(policy, records, poll, permitted, wait, *, clock=time.monotonic, progress=lambda e:None):
    roots=[row['comment_root_id'] for row in records if row.get('comment_root_id')]
    if policy['interaction_mode']!='watch' or not roots:
        return dict(enabled=False,seconds=0,polls=0)
    deadline=clock()+7200
    polls=0
    progress(dict(stage='comment_watch_started',seconds=7200,roots=len(roots)))
    while permitted() and clock()<deadline:
        poll();polls+=1
        end=min(deadline,clock()+20)
        while permitted() and clock()<end:wait(min(1,end-clock()))
    return dict(enabled=True,seconds=7200,polls=polls,completed=clock()>=deadline)
