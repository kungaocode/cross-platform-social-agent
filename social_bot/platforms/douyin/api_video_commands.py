"""Deterministic private commands, bound comment mentions and durable completion notices."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

from ...video_jobs import VideoCommand, VideoJobs
from .backend import DouyinAPIError
from ..common.chat import SocialEvent


HELP = ('固定指令：刷视频1条（1～5条）；可加话题，例如「刷视频1条 话题：大肥鱼、小短剧」，'
        '带话题时先检索再处理，不带话题时按推荐顺序；刷视频进度。整批结束后刷新推荐并私信提醒。')


def fixed_command(text):
    text = text.strip()
    if text == '刷视频进度':
        return VideoCommand('status', 'douyin')
    if not text.startswith('刷视频'):
        return None
    tail = text[len('刷视频'):]
    if '\n' in tail or '\r' in tail:
        return None
    body = tail.strip(' \t')
    count, topics_text = 1, ''
    counted = re.fullmatch(r'([1-5])[ \t]*条(?:[ \t]+(.*))?', body)
    if counted:
        count, topics_text = int(counted[1]), (counted[2] or '').strip()
    else:
        if re.fullmatch(r'[0-9]{1,2}[ \t]*(?:条|轮)', body):
            return None
        topics_text = body
    if not topics_text:
        return VideoCommand('start', 'douyin', rounds=count)
    titled = re.fullmatch(r'(?:话题|兴趣|主题)[：:][ \t]*(.+)', topics_text)
    if titled:
        topics_text = titled[1].strip()
    if re.search(r'[\[\]@＠]', topics_text) or '刷视频' in topics_text or re.search(r'[0-9]+\s*(?:条|轮)', topics_text):
        return None
    topics = tuple(part.strip() for part in re.split(r'[、,，;；\s]+', topics_text) if part.strip())
    if not 1 <= len(topics) <= 8 or any(len(part) > 100 for part in topics):
        return None
    return VideoCommand('start', 'douyin', rounds=count, topics=topics)


def completion_text(report, state, *, message_mode=False, sent=0):
    records = report.get('records', [])
    skipped = report.get('skipped', [])
    processed = report.get('attempted', report.get('opened', len(records)))
    order = '按检索结果' if report.get('selection_mode') == 'search_order' else '按推荐顺序'
    uncertain = 0
    if message_mode:
        text = f"刷视频任务已结束：{order}处理{processed}条，已私信{sent}条解读@你。"
        failed = sum(r.get('analysis_success') is not True for r in records)
        if failed:
            text += f'{failed}条取材或理解失败。'
    else:
        posted = [r for r in records if r.get('publication') == 'published' and r.get('mention_confirmed') is True]
        unverified = sum(r.get('analysis_verified') is not True for r in records)
        uncertain = sum(r.get('publication') == 'unknown_no_retry' for r in records)
        text = f"刷视频任务已结束：{order}处理{processed}条，在{len(posted)}条视频评论区@你并附上解读。"
        if unverified:
            text += f'其中{unverified}条未通过理解复核或取材失败，未发布解读。'
    long_skips = sum(r.get('reason') == 'DOUYIN_VIDEO_TOO_LONG' for r in skipped)
    unavailable = sum(r.get('reason') in ('DOUYIN_VIDEO_UNAVAILABLE','DOUYIN_VIDEO_DURATION_UNKNOWN','DOUYIN_VIDEO_MISMATCH')
                      for r in skipped)
    if long_skips:
        text += f'跳过{long_skips}条超过5分钟的视频。'
    if unavailable:
        text += f'跳过{unavailable}条详情不可用的视频。'
    live = report.get('live_skipped') or 0
    if live:
        text += f'跳过{live}条直播。'
    if uncertain:
        text += f'{uncertain}条发布结果不明，未自动重发。'
    if not message_mode:
        failures = sum(r.get('publication') in ('failed_before_send', 'comment_generation_failed') for r in records)
        if failures:
            text += f'{failures}条评论生成或发布失败。'
        if not posted:
            text += '本次没有确认成功的@解读评论。'
    if state == 'unknown' or report.get('job_phase') == 'failed':
        text += '任务异常结束。'
    if report.get('reason') == 'no_matching_or_visible_candidate':
        text += '推荐流中没有找到匹配候选。'
    if report.get('shortfall'):
        text += f"本批推荐不足或任务提前结束，少处理{report['shortfall']}条。"
    refresh = report.get('recommendation_refresh') or {}
    if refresh.get('success') is True:
        text += '推荐列表已刷新。'
    elif report.get('selection_mode') == 'recommendation_order':
        text += '推荐列表未确认刷新。'
    links = [] if message_mode else [r.get('url', '') for r in posted[:5]]
    return (text+ ('\n'+ '\n'.join(links) if links else ''))[:1000]


def understanding_text(record, name):
    """One @-addressed private message carrying the light understanding of one video."""
    body = record.get('understanding') if isinstance(record, dict) else ''
    if not isinstance(body, str) or not body.strip():
        return ''
    title = str(record.get('title') or '').strip()
    url = str(record.get('url') or '').strip()
    lines = ['@' + (str(name or '').strip() or '你') + ' 视频解读']
    if title:
        lines.append('标题：' + title[:120])
    lines.append(body.strip()[:700])
    if url:
        lines.append(url)
    return '\n'.join(lines)[:1000]


class APIVideoCommands:
    def __init__(self, pump, service, config, path, *, jobs=None):
        self.pump, self.db, self.config, self.path = pump, pump.db, config, Path(path).resolve()
        self.message_mode = config.get('video_social', {}).get('delivery') == 'message'
        service.video_fixed_commands_only = True
        if jobs is None:
            service._video_jobs = service._video_jobs or VideoJobs(Path(config['memory_dir']))
            jobs = service._video_jobs
        self.jobs = jobs
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS douyin_video_routes (
                event_key TEXT PRIMARY KEY,payload TEXT NOT NULL,job_id TEXT,notified INTEGER NOT NULL DEFAULT 0)''')

    def owner(self, event):
        return hashlib.sha256(f'douyin:{event.account}:{event.sender}'.encode()).hexdigest()

    def deliver(self, event, text, *, claimed=False):
        with self.db:
            if not claimed and not self.db.execute(
                    "INSERT OR IGNORE INTO social_deliveries VALUES (?,'processing',NULL)", (event.key,)).rowcount:
                return self.db.execute('SELECT state FROM social_deliveries WHERE event_key=?',(event.key,)).fetchone()[0]
            self.db.execute("UPDATE social_deliveries SET state='sending',reply=? WHERE event_key=?", (text, event.key))
        try:
            self.pump.send(event, text)
        except BaseException:
            with self.db:
                self.db.execute("UPDATE social_deliveries SET state='unknown' WHERE event_key=?", (event.key,))
            raise
        with self.db:
            self.db.execute("UPDATE social_deliveries SET state='sent' WHERE event_key=?", (event.key,))
        return 'sent'

    def handle(self, event):
        if event.channel != 'private' or not self.pump.responder.allowed(event):
            return False
        command = fixed_command(event.text)
        if command is None:
            if event.text.strip().startswith(('刷视频', '[刷视频]')):
                self.deliver(event, HELP)
                return True
            return False
        with self.db:
            if not self.db.execute("INSERT OR IGNORE INTO social_deliveries VALUES (?,'processing',NULL)",
                                   (event.key,)).rowcount:
                return True
        if command.action == 'status':
            status = self.jobs.status(self.owner(event))
            labels = dict(no_job='暂无任务', starting='启动中', running='运行中', completed='已结束', failed='已结束（有失败）', unknown='结果待核实')
            if self.message_mode:
                detail = (f"；已尝试{status.get('attempted', 0)}条，已理解{status.get('understandings', 0)}条，"
                          f"解读私信{'已发出' if status.get('state') == 'completed' else '待发出'}。")
            else:
                detail = (f"；已尝试{status.get('attempted', 0)}条，理解通过{status.get('verified_analyses', 0)}条，"
                          f"发布{status.get('published', 0)}条。")
            self.deliver(event, '刷视频状态：'+labels.get(status['state'], status['state'])+detail, claimed=True)
            return True
        if not self.pump.permitted() or self.config.get('video_social', {}).get('enabled') is not True:
            self.deliver(event, '当前未开启刷视频。', claimed=True)
            return True
        if self.message_mode:
            # Understanding is delivered by private message; no public comment and no mention entity is needed.
            target, command = None, replace(command, post_comments=False)
        else:
            try:
                target = self.pump.api.mention_target(event.sender)
            except DouyinAPIError:
                self.deliver(event, '无法核验你的评论区@身份，本次未启动刷视频，请检查联系人绑定。', claimed=True)
                return True
            # Audit line: the job is bound to the sender, never to a configured default owner.
            print(json.dumps(dict(event='douyin_video_target', sender=event.sender,
                mention_uid=target['uid'], mention_name=target['name']), ensure_ascii=False), flush=True)
        # Durable route precedes the child-process launch. Poll recovers a launch/route-update crash.
        payload = json.dumps(replace(event, text='').__dict__, ensure_ascii=False)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO douyin_video_routes(event_key,payload) VALUES (?,?)', (event.key, payload))
        key = hashlib.sha256(event.key.encode()).hexdigest()
        result = self.jobs.start(command, owner=self.owner(event), event_key=key,
            topics=list(command.topics), config_path=self.path, share_target=target)
        with self.db:
            self.db.execute('UPDATE douyin_video_routes SET job_id=?,notified=? WHERE event_key=?',
                (result.get('job_id'), 0 if result.get('job_id') else 1, event.key))
        if result.get('success'):
            if self.message_mode:
                text = (f'已启动：按推荐顺序处理{command.rounds}条视频，理解完成后私信@你并附标题与画面要点；'
                        '整批结束后刷新推荐。')
            elif command.topics:
                text = (f'已启动：检索「{"、".join(command.topics)}」相关视频{command.rounds}条，'
                        '理解完成后在评论区@你并给出评论；整批结束后私信提醒。')
            else:
                text = (f'已启动：按推荐顺序处理{command.rounds}条视频，理解完成后在评论区@你并给出评论；'
                        '整批结束后刷新推荐并私信提醒。')
        elif result.get('reason') == 'video_platform_busy_or_uncertain':
            text = '当前已有刷视频任务在运行或结果待核实，本次未重复启动。'
        else:
            text = '刷视频任务未能启动，请检查配置。'
        self.deliver(event, text, claimed=True)
        return True

    def send_understandings(self, event, report, job_id):
        """Send one @-addressed private message per analysed video; unknown results are never retried."""
        sent = 0
        for index, record in enumerate(report.get('records') or []):
            text = understanding_text(record, event.sender_name)
            if not text:
                continue
            note = replace(event, id=f'video-understanding:{job_id}:{index}', text='')
            try:
                delivered = self.deliver(note, text)
            except DouyinAPIError as exc:
                if exc.code != 'DOUYIN_SEND_RESULT_UNCONFIRMED':
                    raise
                delivered = 'unknown'
            if delivered == 'sent':
                sent += 1
        return sent

    def poll(self):
        with self.jobs.lock:
            self.jobs._refresh()
            routes = self.db.execute('SELECT event_key,payload,job_id FROM douyin_video_routes WHERE notified=0').fetchall()
            for key, payload, job_id in routes:
                event = SocialEvent(**json.loads(payload))
                job = self.jobs.db.execute('SELECT * FROM jobs WHERE '+('id=?' if job_id else 'event_key=?'),
                    (job_id or hashlib.sha256(key.encode()).hexdigest(),)).fetchone()
                if not job or job['owner'] != self.owner(event) or job['state'] in ('starting', 'running'):
                    continue
                report_path = self.jobs.directory/(job['id']+'.result.json')
                report = {}
                if report_path.is_file() and report_path.stat().st_size < 1_000_000:
                    try:
                        report = json.loads(report_path.read_text(encoding='utf-8'))
                    except (OSError, ValueError):
                        pass
                notice = replace(event, id='video-complete:'+job['id'], text='')
                if not self.pump.permitted():
                    return
                send_understanding = (self.message_mode
                    or self.config.get('video_social', {}).get('understanding_message') is True)
                sent = self.send_understandings(event, report, job['id']) if send_understanding else 0
                try:
                    delivered = self.deliver(notice, completion_text(report, job['state'],
                        message_mode=self.message_mode, sent=sent))
                except DouyinAPIError as exc:
                    if exc.code != 'DOUYIN_SEND_RESULT_UNCONFIRMED':
                        raise
                    delivered = 'unknown'
                notified = 1 if delivered == 'sent' else -1
                with self.db:
                    self.db.execute('UPDATE douyin_video_routes SET notified=?,job_id=? WHERE event_key=?', (notified,job['id'],key))
                if notified == -1:
                    print(json.dumps(dict(event='video_completion_delivery_unknown',job_id=job['id'],retry=False)),flush=True)
