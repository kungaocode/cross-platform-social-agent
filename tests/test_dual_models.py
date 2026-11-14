import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

from social_bot.deepseek import DeepSeekClient, DeepSeekError
from social_bot.settings import DeepSeekSettings


def reply(content):
    return {'choices':[{'message':{'role':'assistant','content':content}}]}


def call(name='read'):
    return {'choices':[{'message':dict(role='assistant',content=None,reasoning_content='private reasoning',
        tool_calls=[dict(id='one',type='function',function=dict(name=name,arguments='{}'))])}]}


class DualModelTests(unittest.TestCase):
    def setUp(self):
        self.settings=DeepSeekSettings(collaboration_enabled=True,task_model='deepseek-flash',model='deepseek-chat')
        self.client=DeepSeekClient('unused',self.settings)
        self.messages=[dict(role='system',content='use our personality'),dict(role='user',content='question')]

    def test_text_analysis_then_chat_final_without_tools(self):
        self.client._request=Mock(side_effect=[reply('facts only'),reply('final chat')])
        self.assertEqual(self.client.complete(self.messages),'final chat')
        requests=[c.args[0] for c in self.client._request.call_args_list]
        self.assertEqual([r['model'] for r in requests],['deepseek-flash','deepseek-chat'])
        self.assertEqual(requests[0]['max_tokens'],2400)
        self.assertEqual(requests[0]['_timeout_seconds'],90)
        self.assertNotIn('tools',requests[1])
        self.assertEqual(requests[1]['messages'][0],self.messages[0])
        self.assertIn('facts only',requests[1]['messages'][-1]['content'])

    def test_worker_executes_once_and_hands_real_receipt_to_chat(self):
        self.client._request=Mock(side_effect=[call(),reply('summary'),reply('chat result')])
        execute=Mock(return_value=dict(success=True,side_effect_performed=True,message_sent=True,text='sent'))
        result=self.client.complete_with_tools(self.messages,[dict(type='function',function=dict(name='read'))],execute)
        self.assertEqual(result.content,'chat result')
        self.assertEqual(result.tool_calls,1)
        self.assertTrue(result.side_effect_performed)
        execute.assert_called_once()
        requests=[c.args[0] for c in self.client._request.call_args_list]
        self.assertEqual([r['model'] for r in requests],['deepseek-flash','deepseek-flash','deepseek-chat'])
        final=requests[-1]
        self.assertNotIn('tools',final)
        self.assertNotIn('private reasoning',json.dumps(final))
        self.assertTrue(all(m['role']!='tool' and 'tool_calls' not in m for m in final['messages']))
        self.assertIn('message_sent',final['messages'][-1]['content'])

    def test_tool_exception_retained_as_failure_and_not_reexecuted(self):
        self.client._request=Mock(side_effect=[call(),reply('failed'),reply('failure explained')])
        execute=Mock(side_effect=RuntimeError('failure'))
        result=self.client.complete_with_tools(self.messages,[dict(type='function',function=dict(name='read'))],execute)
        self.assertEqual(result.content,'failure explained')
        self.assertFalse(result.side_effect_performed)
        execute.assert_called_once()
        self.assertIn('tool_error:RuntimeError',self.client._request.call_args.args[0]['messages'][-1]['content'])

    def test_chat_error_does_not_fall_back_to_flash_output(self):
        self.client._request=Mock(side_effect=[reply('internal work'),DeepSeekError('chat unavailable')])
        with self.assertRaisesRegex(DeepSeekError,'chat unavailable'):
            self.client.complete(self.messages)
        self.assertEqual(self.client._request.call_count,2)

    def test_task_error_does_not_generate_unverified_chat(self):
        self.client._request=Mock(side_effect=DeepSeekError('task unavailable'))
        with self.assertRaises(DeepSeekError):self.client.complete(self.messages)
        self.assertEqual(self.client._request.call_count,1)

    def test_final_tool_call_is_rejected(self):
        self.client._request=Mock(side_effect=[reply('brief'),call()])
        with self.assertRaisesRegex(DeepSeekError,'最终聊天模型'):
            self.client.complete(self.messages)

    def test_complete_task_only_uses_flash(self):
        self.client._request=Mock(return_value=reply('analysis'))
        self.assertEqual(self.client.complete_task(self.messages),'analysis')
        self.assertEqual(self.client._request.call_args.args[0]['model'],'deepseek-flash')
        self.assertEqual(self.client._request.call_count,1)

    def test_concurrent_requests_do_not_mix_handoffs(self):
        def request(payload):
            if payload['model']=='deepseek-flash':
                return reply('brief '+payload['messages'][0]['content'])
            package=json.loads(payload['messages'][-1]['content'].split('\n',1)[1])
            return reply(package['brief'])
        self.client._request=request
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda i:self.client.complete([dict(role='user',content=str(i))]),range(30)))
        self.assertEqual(results,['brief '+str(i) for i in range(30)])

    def test_disabling_collaboration_keeps_single_model_mode(self):
        client=DeepSeekClient('unused',replace(self.settings,collaboration_enabled=False))
        client._request=Mock(return_value=reply('chat'))
        self.assertEqual(client.complete(self.messages),'chat')
        self.assertEqual(client._request.call_count,1)

    def test_worker_can_continue_video_tools_then_chat(self):
        self.client._request=Mock(side_effect=[call('video_list'),call('video_next'),reply('prepared'),reply('final')])
        execute=Mock(return_value=dict(success=True))
        names=frozenset({'video_list','video_next'})
        result=self.client.complete_with_tools(self.messages,[dict(type='function',function=dict(name=n)) for n in names],
            execute,max_rounds=4,continuable_tools=names)
        self.assertEqual(result.content,'final')
        self.assertEqual(execute.call_count,2)

    def test_evidence_is_bounded_and_truncation_marked(self):
        self.client._request=Mock(return_value=reply('final'))
        self.client._final_reply(self.messages,'b'*13000,[('read',dict(success=False,text='x'*50000))])
        package=json.loads(self.client._request.call_args.args[0]['messages'][-1]['content'].split('\n',1)[1])
        self.assertTrue(package['brief_truncated'])
        self.assertTrue(package['tool_results'][0]['truncated'])
        self.assertFalse(package['tool_results'][0]['success'])

    def test_truncated_task_result_is_not_treated_as_complete(self):
        response=reply('partial')
        response['choices'][0]['finish_reason']='length'
        self.client._request=Mock(return_value=response)
        with self.assertRaisesRegex(DeepSeekError,'截断'):
            self.client.complete(self.messages)
        self.assertEqual(self.client._request.call_count,1)
