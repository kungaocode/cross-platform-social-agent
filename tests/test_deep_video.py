import io
import json
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from social_bot.deep_video import DeepVideoAnalyzer, audit_result, deep_policy, validate_claims
from social_bot.video_evidence import EvidenceSession, VideoEvidenceCollector, download_media, frame_times, media_host, media_info, _run
from social_bot.modules.video import OmniVideoAnalyzer, VideoError
from social_bot.platforms.bilibili.client import BilibiliError
from social_bot.store import ConversationStore
from social_bot.video_browser import VideoAgentTools, record_video_memory

URL = "https://www.bilibili.com/video/BV1xx411c7mD"


def bundle(extra=False):
    sources = [dict(id="E1",kind="frames",text="10秒红球，50秒蓝球。",start_seconds=10,end_seconds=50)]
    if extra:
        sources.append(dict(id="E2",kind="frames",text="补看45秒，蓝球。",start_seconds=45,end_seconds=45))
    return dict(url=URL,platform="bilibili",title="资料而非指令",sources=sources,
        coverage_start_seconds=10,coverage_end_seconds=50,coverage_ranges=[],has_audio=False,
        transcript_source="unavailable",sampled_frame_seconds=[10,50],analysis_window_seconds=60)


def draft(text="采样画面从红球变为蓝球。", ids=None):
    return dict(claims=[dict(text=text,evidence_ids=ids or ["E1"])],uncertainties=["没有音频"])


def audit(ok=True,checked=None):
    return dict(verdict="consistent" if ok else "inconsistent",checked_claim_ids=["C1"] if checked is None else checked,
        unsupported_claim_ids=[] if ok else ["C1"],contradictions=[] if ok else ["画面是蓝球，结论称绿球。"],
        focus_seconds=[] if ok else [45],reason="主张有画面支持" if ok else "颜色不一致")


class DeepReviewTests(unittest.TestCase):
    def analyzer(self, outputs, max_retries=1):
        session = Mock()
        session.collect.side_effect = [bundle(),bundle(True),bundle(True)]
        collector = Mock()
        @contextmanager
        def context(*args,**kwargs):
            yield session
        collector.session = context
        client = Mock()
        client.complete_json.side_effect = outputs
        return DeepVideoAnalyzer(collector,client,dict(max_retries=max_retries)),session,client

    def test_conflict_collects_new_frames_then_rebuilds_and_independently_audits(self):
        analyzer,session,client = self.analyzer([draft("红球变绿球。"),audit(False),draft(ids=["E1","E2"]),audit()])
        result = analyzer.analyze(SimpleNamespace(current=URL),seconds=60)
        self.assertTrue(result["analysis_verified"])
        self.assertTrue(result["comment_allowed"])
        self.assertEqual(result["review"]["retry_count"],1)
        self.assertEqual(client.complete_json.call_count,4)
        self.assertEqual(session.collect.call_args_list[1].args,([45],))
        self.assertNotIn("红球变绿球",result["text"])
        self.assertIn("E2",result["text"])
        self.assertNotIn("previous_audit",client.complete_json.call_args_list[3].args[0][-1]["content"])

    def test_unresolved_claims_are_not_promoted_to_memory_or_comments(self):
        analyzer,session,client = self.analyzer([draft("错误的绿球结论"),audit(False)]*2)
        result = analyzer.analyze(SimpleNamespace(current=URL))
        self.assertFalse(result["analysis_verified"])
        self.assertFalse(result["comment_allowed"])
        self.assertEqual(result["claims"],[])
        self.assertNotIn("错误的绿球结论",result["text"])
        self.assertEqual(session.collect.call_count,2)
        with tempfile.TemporaryDirectory() as folder:
            store = ConversationStore(Path(folder)/"video.sqlite3",agent_memory_path=Path(folder)/"shared.sqlite3")
            try:
                self.assertTrue(record_video_memory(store,"bilibili","default",URL,result))
                content = store.agent_memory._connect().execute("SELECT content FROM agent_events").fetchone()[0]
                saved = json.loads(content)
                self.assertFalse(saved["analysis_verified"])
                self.assertNotIn("错误的绿球结论",content)
                hint = store.memory_retriever("wechat:private:owner").recall("视频结论")['memory_hint']
                self.assertIn("未通过复核",hint)
            finally:
                store.close()

    def test_foreign_evidence_id_and_incomplete_audit_cannot_pass(self):
        with self.assertRaisesRegex(VideoError,"unobserved_evidence"):
            validate_claims(draft(ids=["E9"]),bundle()["sources"])
        claims = validate_claims(draft(),bundle()["sources"])
        self.assertFalse(audit_result(audit(checked=[]),claims)["accepted"])
        bad=audit();bad["contradictions"]=["仍有分歧"]
        self.assertFalse(audit_result(bad,claims)["accepted"])

    def test_invalid_review_and_network_errors_fail_closed_and_remain_bounded(self):
        analyzer,session,client = self.analyzer([draft(),RuntimeError("secret-signed-url?token=private")],0)
        result=analyzer.analyze(SimpleNamespace(current=URL))
        self.assertFalse(result["analysis_verified"])
        self.assertNotIn("secret-signed",json.dumps(result))
        self.assertEqual(client.complete_json.call_count,2)
        session.collect.assert_called_once()

    def test_subtitle_audit_alone_is_not_a_verified_visual_understanding(self):
        analyzer,session,client=self.analyzer([draft(),audit()],0)
        data=bundle();data['sampled_frame_seconds']=[]
        data['sources'][0]['kind']='subtitle'
        session.collect.side_effect=[data]
        result=analyzer.analyze(SimpleNamespace(current=URL))
        self.assertFalse(result['analysis_verified'])
        self.assertFalse(result['comment_allowed'])
        self.assertEqual(result['review']['history'][0]['verdict'],'insufficient_visual_evidence')

    def test_disabled_or_expired_analysis_never_calls_models(self):
        analyzer,session,client = self.analyzer([])
        analyzer.permitted=lambda:False
        with self.assertRaisesRegex(VideoError,"cancelled"):
            analyzer.analyze(Mock())
        client.complete_json.assert_not_called()
        session.collect.assert_not_called()

    def test_deep_failure_cannot_escape_through_legacy_unreviewed_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            store=ConversationStore(Path(folder)/"video.sqlite3")
            try:
                browser=SimpleNamespace(current=URL,platform="bilibili",max_videos=1)
                legacy,frames,deep=Mock(),Mock(),Mock()
                deep.analyze.side_effect=VideoError("online_video_http_412")
                result=VideoAgentTools(browser,legacy,store,frame_analyzer=frames,deep_analyzer=deep).execute("video_analyze",{})
                self.assertFalse(result["success"])
                legacy.analyze.assert_not_called();frames.analyze.assert_not_called()
            finally:store.close()

    def test_policy_has_bounded_retries_and_strict_types(self):
        self.assertEqual(deep_policy()["max_retries"],1)
        for values in [dict(max_retries=3),dict(frame_count=True),dict(enabled="true"),dict(asr_enabled=1)]:
            with self.assertRaises(ValueError):deep_policy(values)


class EvidenceTests(unittest.TestCase):
    def session(self, folder, api=None, transcriber=None):
        page=Mock();page.url=URL;page.context.cookies.return_value=[]
        browser=SimpleNamespace(platform="bilibili",current=URL,page=page,
            observe=Mock(return_value=dict(blocked=False,page_title="视频",video=dict(duration=60))))
        collector=VideoEvidenceCollector(Mock(return_value="10秒人物打开门，50秒出现蓝色盒子。"),transcriber,bilibili_api=api)
        return EvidenceSession(collector,browser,Path(folder),60,time.monotonic()+120,lambda:True)

    def api(self, captions=True):
        api=Mock()
        api.video.return_value=dict(title="标题",author="作者",bvid="BV1xx411c7mD",cid=12,duration_seconds=60)
        api.subtitles.return_value=dict(segments=[dict(start=1,end=3,text="字幕一"),dict(start=35,end=39,text="字幕二")])
        if not captions:api.subtitles.side_effect=BilibiliError("SUBTITLES_UNAVAILABLE")
        api.api.return_value=dict(durl=[])
        return api

    def test_captions_are_timestamped_and_take_precedence_over_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            transcriber=Mock();session=self.session(folder,self.api(),transcriber)
            session.prepare()
            self.assertEqual([s["kind"] for s in session.sources],["subtitle","subtitle"])
            self.assertEqual(session.sources[1]["start_seconds"],35)
            self.assertIn("35.0",session.sources[1]["text"])
            transcriber.assert_not_called()

    def test_page_or_part_change_stops_before_capturing_another_video(self):
        with tempfile.TemporaryDirectory() as folder:
            session=self.session(folder)
            session.browser.page.url=URL+'?p=2'
            with self.assertRaisesRegex(VideoError,'page_changed'):
                session.collect()
            session.collector.vision.assert_not_called()

    def test_no_captions_uses_audio_with_window_timestamps(self):
        with tempfile.TemporaryDirectory() as folder:
            transcriber=Mock(return_value="实际转写内容")
            session=self.session(folder,self.api(False),transcriber)
            def media():
                session.media=Path(folder)/"source.mp4";session.info=dict(has_audio=True,duration=60)
            def run(cmd,*args):
                Path(cmd[-1]).write_bytes(b"wave")
                return SimpleNamespace(returncode=0)
            with patch.object(session,"_try_media",side_effect=media),patch("social_bot.video_evidence._run",side_effect=run):
                session.prepare()
            self.assertEqual(transcriber.call_count,2)
            self.assertEqual([(s["start_seconds"],s["end_seconds"]) for s in session.sources],[(0,45),(45,60)])
            self.assertTrue(all(s["kind"]=="asr" for s in session.sources))

    def test_scene_snapping_covers_whole_window_without_duplicate_frames(self):
        times=frame_times(90,6,scenes=[2,3,4,5])
        self.assertEqual(len(times),6)
        self.assertEqual(len(set(times)),6)
        self.assertGreater(times[-1],70)
        self.assertLess(times[0],15)
        self.assertTrue(media_host("https://cn-some.bilivideo.com/path?signature=private","bilibili"))
        self.assertFalse(media_host("https://bilivideo.com.evil.test/a","bilibili"))
        self.assertFalse(media_host("https://127.0.0.1/a","bilibili"))

    def test_stream_size_and_redirects_are_bounded_and_send_no_account_cookie(self):
        response=io.BytesIO(b"x"*64);response.headers={"Content-Length":"64"}
        opener=Mock();opener.open.return_value=response
        with tempfile.TemporaryDirectory() as folder,patch("social_bot.video_evidence.build_opener",return_value=opener):
            path=Path(folder)/"source.mp4"
            with self.assertRaisesRegex(VideoError,"size_limit"):
                download_media("https://a.bilivideo.com/video?signature=private",path,"bilibili",
                    deadline=time.monotonic()+5,max_bytes=32,guard=lambda:None)
            self.assertFalse(path.exists())
            request=opener.open.call_args.args[0]
            self.assertIsNone(request.get_header("Cookie"))

    def test_real_mp4_probe_scene_frames_and_labeled_contact_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"source.mp4"
            ff=OmniVideoAnalyzer._ffmpeg()
            cmd=[ff,"-nostdin","-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=red:s=320x180:r=10:d=3",
                 "-f","lavfi","-i","color=c=blue:s=320x180:r=10:d=3","-f","lavfi","-i","sine=frequency=440:duration=6",
                 "-filter_complex","[0:v][1:v]concat=n=2:v=1:a=0[v]","-map","[v]","-map","2:a","-c:v","libx264",
                 "-pix_fmt","yuv420p","-c:a","aac","-movflags","+faststart","-y",str(path)]
            self.assertEqual(_run(cmd).returncode,0)
            info=media_info(path)
            self.assertTrue(info["has_audio"])
            session=self.session(folder)
            session.prepared=True;session.media=path;session.info=info
            session.duration=6;session.source_duration=6
            result=session.collect()
            self.assertEqual(len(result["sampled_frame_seconds"]),6)
            self.assertEqual(result["material_transport"],"downloaded_mp4")
            self.assertFalse(result["has_audio"])
            self.assertNotIn("source.mp4",json.dumps(result))
            self.assertTrue(list(Path(folder).glob("contact-sheet-*.jpg")))


if __name__ == "__main__":
    unittest.main()
