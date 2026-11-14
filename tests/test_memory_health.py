import json
import tempfile
import unittest
from pathlib import Path

from social_bot.agent_memory import AgentMemory
from social_bot.memory_health import check_memory
from social_bot.store import ConversationStore


class MemoryHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.generation=self.root/'generation';self.generation.mkdir()
        self.shared=self.generation/'agent-memory.sqlite3'
        self.config=self.root/'config.json'
        self.config.write_text(json.dumps(dict(runtime=dict(agent_memory_enabled=True,
            agent_memory_path=str(self.shared),database_path=str(self.generation/'wechat-default.sqlite3')))),encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_shared_database_is_reported_without_creating_it(self):
        result=check_memory(self.config)
        self.assertFalse(result['ok'])
        self.assertIn('shared_database_missing',result['errors'])
        self.assertFalse(self.shared.exists())

    def test_health_checks_replication_without_exposing_or_modifying_messages(self):
        store=ConversationStore(self.generation/'wechat-default.sqlite3',agent_memory_path=self.shared)
        try:
            store.add('wechat:private:owner','user','PRIVATE_BODY_NOT_IN_REPORT','m',sender_id='owner')
            before=store.connection.total_changes
            result=check_memory(self.config)
            self.assertTrue(result['ok'],result)
            self.assertEqual(result['sources'][0]['missing_shared_events'],0)
            self.assertNotIn('PRIVATE_BODY_NOT_IN_REPORT',json.dumps(result))
            self.assertEqual(store.connection.total_changes,before)
            with store.agent_memory._lock,store.agent_memory._connect():
                store.agent_memory._connect().execute('DELETE FROM agent_events')
            missing=check_memory(self.config)
            self.assertEqual(missing['sources'][0]['missing_shared_events'],1)
            self.assertIn('wechat-default.sqlite3:messages_not_in_shared_memory',missing['warnings'])
        finally:
            store.close()

    def test_platform_base_config_and_manifest_drift_are_detected(self):
        hub=AgentMemory(self.shared);hub.stats();hub.close()
        (self.root/'qq').mkdir()
        wrong=self.root/'qq'/'wrong-base.json'
        wrong.write_text(json.dumps(dict(runtime=dict(agent_memory_enabled=True,agent_memory_path='other.sqlite3'))))
        (self.root/'qq'/'config.json').write_text(json.dumps(dict(base_config='wrong-base.json',memory_dir=str(self.generation))))
        (self.root/'current.json').write_text(json.dumps(dict(generation=str(self.generation),agent_memory_database=str(self.root/'wrong.sqlite3'))))
        result=check_memory(self.config)
        self.assertFalse(result['ok'])
        self.assertIn('qq:shared_config_mismatch',result['errors'])
        self.assertIn('manifest_shared_path_mismatch',result['errors'])


if __name__=='__main__':
    unittest.main()
