"""Synthetic health checks. All outbound transport and time are controlled."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from skald import health
from skald.__main__ import main
from decide_kit import load_server, close_server
from test_api import raw_request


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.now = 172800.0
        self.send = Mock(return_value=True)
        self.h = health.Health(self.data, clock=lambda: self.now, transport=self.send)
        self.settings = {'enabled': True, 'repair': True, 'cooldown': 600, 'max_attempts': 2,
                         'disk_min_bytes': 100, 'connector_failures': 3}
        self.write('health-settings.json', self.settings)
        self.snapshot = {'worker_alive': True, 'feeds': {}, 'connectors': {},
                         'budget_ratio': 0, 'budget_over': False, 'aio_pending': False,
                         'disk_free': 1000, 'rows': {}}

    def write(self, name, value):
        path = self.data / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value))

    def tick(self, actions=None):
        return self.h.tick(self.snapshot, actions or {})

    def bad(self, check):
        return next(c for c in self.tick()['checks'] if c['id'] == check)['state'] != 'ok'

    def channel(self):
        self.write('credentials/alerts.json', {'telegram': {'token': 'FAKE-TOKEN', 'chat_id': 'FAKE-CHAT'},
                                              'webhook': {'url': 'https://example.invalid/FAKE-URL', 'style': 'discord'}})

    def test_off_by_default(self):
        (self.data / 'health-settings.json').unlink()
        self.snapshot['worker_alive'] = False
        restart = Mock()
        self.assertFalse(self.tick({'worker': restart})['enabled'])
        restart.assert_not_called()
        self.assertFalse((self.data / 'health.jsonl').exists())

    def test_checks(self):
        self.snapshot.update(worker_alive=False, feeds={'feed:one': {'built': self.now-1000, 'interval': 300}},
                             budget_ratio=.91, budget_over=True, aio_pending=True, disk_free=99)
        result = {c['id']: c['state'] for c in self.tick()['checks']}
        for key in ('worker', 'feed:one', 'budget', 'aiostreams', 'disk'):
            self.assertNotEqual(result[key], 'ok')
        self.snapshot['budget_over'] = False
        self.assertTrue(self.bad('budget'))
        self.snapshot['budget_ratio'] = .5
        self.assertFalse(self.bad('budget'))

    def test_connector_counts_actual_consecutive_polls(self):
        for polls in (1, 1, 2):
            self.snapshot['connectors'] = {'connector:one': {'polls': polls, 'failed': True}}
            self.assertFalse(self.bad('connector:one'))
        self.snapshot['connectors']['connector:one']['polls'] = 3
        self.assertTrue(self.bad('connector:one'))
        self.snapshot['connectors']['connector:one'].update(polls=4, failed=False)
        self.assertFalse(self.bad('connector:one'))

    def test_rows_only_warn_after_nonempty_serving(self):
        self.snapshot['rows'] = {'row:one': 0}
        self.assertFalse(self.bad('row:one'))
        self.snapshot['rows']['row:one'] = 2
        self.tick()
        self.snapshot['rows']['row:one'] = 0
        self.assertTrue(self.bad('row:one'))

    def test_cooldown_daily_limit_and_restart_persistence(self):
        self.snapshot['worker_alive'] = False
        repair = Mock(return_value=False)
        self.tick({'worker': repair})
        self.tick({'worker': repair})
        self.assertEqual(repair.call_count, 1)
        self.h = health.Health(self.data, clock=lambda: self.now, transport=self.send)
        self.now += 600
        self.tick({'worker': repair})
        self.now += 1200
        self.tick({'worker': repair})
        self.assertEqual(repair.call_count, 2)
        self.now += 86400
        self.tick({'worker': repair})
        self.assertEqual(repair.call_count, 3)
        lines = (self.data / 'health.jsonl').read_text().splitlines()
        self.assertEqual(len(lines), 3)

    def test_midnight_resets_daily_limit_but_keeps_cooldown(self):
        self.settings.update(cooldown=1800, max_attempts=1)
        self.write('health-settings.json', self.settings)
        self.snapshot['worker_alive'] = False
        repair = Mock(return_value=False)
        self.now = 3 * 86400 - 60
        self.tick({'worker': repair})
        self.now += 300
        self.h = health.Health(self.data, clock=lambda: self.now, transport=self.send)
        self.tick({'worker': repair})
        self.assertEqual(repair.call_count, 1)
        self.now += 1500
        self.tick({'worker': repair})
        self.assertEqual(repair.call_count, 2)

    def test_successful_and_pending_repairs_do_not_alert(self):
        self.channel()
        self.snapshot['worker_alive'] = False
        self.tick({'worker': Mock(return_value=True)})
        self.send.assert_not_called()
        self.now += 300
        self.tick({'worker': Mock(return_value='pending')})
        self.assertTrue(self.send.called)  # previous success did not clear the check

    def test_pending_repair_waits_for_next_observation(self):
        self.channel()
        self.snapshot['worker_alive'] = False
        self.tick({'worker': Mock(return_value='pending')})
        self.send.assert_not_called()
        self.now += 300
        self.tick()
        self.assertEqual(self.send.call_count, 2)

    def test_alert_dedupe_recovery_and_persistence(self):
        self.channel()
        self.snapshot['disk_free'] = 0
        self.tick()
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.h = health.Health(self.data, clock=lambda: self.now, transport=self.send)
        self.snapshot['disk_free'] = 1000
        self.tick()
        self.tick()
        self.assertEqual(self.send.call_count, 4)
        self.assertIn('recovered', self.send.call_args.args[1].lower())

    def test_failed_delivery_retries(self):
        self.channel()
        self.send.side_effect = ValueError('FAKE-TOKEN FAKE-CHAT https://example.invalid/FAKE-URL')
        self.snapshot['disk_free'] = 0
        self.tick()
        self.send.side_effect = None
        self.tick()
        self.assertEqual(self.send.call_count, 4)
        self.assert_secrets_absent()

    def test_quiet_hours_defer_alert_and_recovery(self):
        self.channel()
        self.settings.update(quiet_hours='00-06', timezone='UTC')
        self.write('health-settings.json', self.settings)
        self.snapshot['disk_free'] = 0
        self.tick()
        self.send.assert_not_called()
        self.now += 7*3600
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.now += 17*3600
        self.snapshot['disk_free'] = 1000
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.now += 7*3600
        self.tick()
        self.assertEqual(self.send.call_count, 4)

    def test_daily_digest_opt_in(self):
        self.channel()
        self.tick()
        self.now += 86400
        self.tick()
        self.send.assert_not_called()
        self.settings['digest'] = True
        self.write('health-settings.json', self.settings)
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.tick()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn('digest', self.send.call_args.args[1].lower())

    def test_json_checks_protect_state_and_restore_cache(self):
        for name in ('taste.json', 'ratings.json', 'history.json', 'skald.json', 'feeds.json', 'shown.json'):
            self.write(name, {})
        self.tick()
        for name in ('taste.json', 'ratings.json', 'history.json', 'skald.json', 'feeds.json', 'shown.json'):
            (self.data / name).write_text('broken FAKE-TOKEN')
        self.write('credentials/private.json', {})
        (self.data / 'credentials/private.json').write_text('broken FAKE-TOKEN')
        self.tick()
        for name in ('taste.json', 'ratings.json', 'history.json', 'skald.json'):
            self.assertEqual((self.data / name).read_text(), 'broken FAKE-TOKEN')
        for name in ('feeds.json', 'shown.json'):
            self.assertEqual(json.loads((self.data / name).read_text()), {})
        self.assertEqual((self.data / 'credentials/private.json').read_text(), 'broken FAKE-TOKEN')
        self.assert_secrets_absent()

    def test_scoped_serving_cache_restores_last_good_copy(self):
        original = {'member': {'row|en-US': {'metas': {}, 'names': {}}}}
        self.write('shown_scopes.json', original)
        self.tick()
        (self.data / 'shown_scopes.json').write_text('{')
        self.tick()
        self.assertEqual(json.loads((self.data / 'shown_scopes.json').read_text()), original)

    def test_pure_cache_deleted_without_good_copy(self):
        (self.data / 'feeds.json').write_text('{')
        self.tick()
        self.assertFalse((self.data / 'feeds.json').exists())

    def test_no_cache_repair_when_disabled(self):
        self.settings['repair'] = False
        self.write('health-settings.json', self.settings)
        (self.data / 'feeds.json').write_text('{')
        self.tick()
        self.assertTrue((self.data / 'feeds.json').exists())

    def test_actions_never_record_exception_details_and_log_is_capped(self):
        self.channel()
        self.snapshot['worker_alive'] = False
        repair = Mock(side_effect=RuntimeError('FAKE-TOKEN FAKE-CHAT https://example.invalid/FAKE-URL'))
        self.tick({'worker': repair})
        self.assert_secrets_absent()
        for _ in range(5000):
            self.h.record('worker', 'failed')
        self.assertLessEqual((self.data / 'health.jsonl').stat().st_size, health.LOG_LIMIT)
        for line in (self.data / 'health.jsonl').read_text().splitlines():
            json.loads(line)

    def assert_secrets_absent(self):
        public = json.dumps(self.tick())
        for name in ('health.jsonl', 'health-state.json', 'health-status.json'):
            path = self.data / name
            if path.exists():
                public += path.read_text()
        for secret in ('FAKE-TOKEN', 'FAKE-CHAT', 'FAKE-URL'):
            self.assertNotIn(secret, public)

    def test_setup_private_channels_and_test(self):
        out = io.StringIO()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}), patch('sys.stdin', io.StringIO('FAKE-TOKEN\n')), patch('skald.health.send_message', self.send):
            self.assertEqual(main(['setup', 'alerts', '--telegram', '--token-stdin', '--chat-id', 'FAKE-CHAT', '--json'], out), 0)
            self.assertEqual(main(['setup', 'alerts', '--test', '--json'], out), 0)
        self.assertEqual((self.data / 'credentials/alerts.json').stat().st_mode & 0o777, 0o600)
        self.assertNotIn('FAKE-', out.getvalue())
        self.assertEqual(self.send.call_count, 1)

    def test_cli_reads_status_without_engine_or_writes(self):
        self.tick()
        out = io.StringIO()
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}), patch('skald.setup.server', side_effect=AssertionError('engine forbidden')):
            self.assertEqual(main(['health', '--json'], out), 0)
        self.assertIn('checks', json.loads(out.getvalue()))

    def test_connector_backoff_and_authoritative_count_persistence(self):
        self.channel()
        self.snapshot['connectors'] = {'connector:one': {'polls': 4, 'failed': True, 'failures': 3}}
        repair = Mock(return_value=False)
        self.tick({'connector:one': repair})
        self.assertEqual(self.h.state['polls']['connector:one']['failures'], 3)
        self.now += 600
        self.tick({'connector:one': repair})
        self.now += 600
        self.tick({'connector:one': repair})
        self.assertEqual(repair.call_count, 2)

    def test_corrupt_health_ledger_disables_repairs(self):
        (self.data / 'health-state.json').write_text('{')
        self.h = health.Health(self.data, clock=lambda: self.now, transport=self.send)
        self.snapshot['worker_alive'] = False
        repair = Mock()
        self.tick({'worker': repair})
        repair.assert_not_called()
        self.assertEqual((self.data / 'health-state.json').read_text(), '{')

    def test_no_protected_cache_action_even_when_called_directly(self):
        for name in ('history.json', 'ratings.json', 'skald.json', 'credentials/private.json', '../private.json'):
            self.assertFalse(self.h.cache_repair(name))

    def test_webhook_setup_and_no_secret_in_errors(self):
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr
        with patch.dict(os.environ, {'DATA_DIR': str(self.data)}), patch('sys.stdin', io.StringIO('https://example.invalid/FAKE-URL\n')), redirect_stderr(err):
            self.assertEqual(main(['setup', 'alerts', '--webhook', '--url-stdin', '--json'], out), 0)
        self.assertNotIn('FAKE-URL', out.getvalue() + err.getvalue())
        self.assertEqual((self.data / 'credentials/alerts.json').stat().st_mode & 0o777, 0o600)

    def test_ntfy_topic_json_transport(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{}')) as transport:
            self.assertTrue(health.send_message({'webhook': {'url': 'https://example.invalid/topic', 'style': 'ntfy'}}, 'Skald test'))
        self.assertEqual(transport.call_args.args[0].full_url, 'https://example.invalid/')
        self.assertEqual(json.loads(transport.call_args.args[0].data)['topic'], 'topic')

    def test_invalid_good_cache_is_not_restored(self):
        self.write('health-cache-feeds.json', {'bad': 'not a serving record'})
        (self.data / 'feeds.json').write_text('{')
        self.tick()
        self.assertFalse((self.data / 'feeds.json').exists())

    def test_one_transport_json_posts(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'{}')) as transport:
            self.assertTrue(health.send_message({'webhook': {'url': 'https://example.invalid/hook', 'style': 'gotify'}}, 'Skald test'))
        req = transport.call_args.args[0]
        self.assertEqual(req.get_method(), 'POST')
        self.assertEqual(json.loads(req.data)['message'], 'Skald test')

    def test_discord_requests_confirmation_with_existing_query(self):
        for query in ('', '?thread_id=123', '?wait=false&thread_id=123', '?thread_id=123#fragment'):
            with self.subTest(query=query):
                channel = {'webhook': {'url': 'https://' + 'example.invalid' + '/hook' + query,
                                       'style': 'discord'}}
                response = io.BytesIO(b'{}')
                response.status = 200
                with patch('urllib.request.urlopen', return_value=response) as transport:
                    self.assertTrue(health.send_message(channel, 'Skald test'))
                request = transport.call_args.args[0]
                parsed = urllib.parse.urlsplit(request.full_url)
                fields = urllib.parse.parse_qs(parsed.query)
                self.assertEqual(fields.get('wait'), ['true'])
                if 'thread_id' in query:
                    self.assertEqual(fields.get('thread_id'), ['123'])
                self.assertEqual(parsed.path, '/hook')
                self.assertEqual(request.get_method(), 'POST')
                self.assertEqual(json.loads(request.data), {'content': 'Skald test'})

    def test_discord_marks_delivered_only_after_2xx(self):
        self.h.transport = None
        channel = {'webhook': {'url': 'https://' + 'example.invalid' + '/hook', 'style': 'discord'}}
        for status in (199, 200, 201, 204, 300, 400, 500):
            with self.subTest(status=status):
                response = io.BytesIO(b'{}')
                response.status = status
                delivered = []
                with patch('skald.health.read_json', return_value=channel), \
                        patch('urllib.request.urlopen', return_value=response):
                    self.assertEqual(self.h.notify('Skald test', delivered), 200 <= status < 300)
                self.assertEqual(delivered, ['webhook'] if 200 <= status < 300 else [])


class ServerHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.s = load_server(self.temp.name)
        self.addCleanup(close_server, self.s)

    def test_forwarded_loopback_health_requires_auth_for_details(self):
        token = self.s.new_client('health-proxy-test', ['read'])
        with patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            for peer in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
                for name in ('X-Forwarded-For', 'X-Real-IP', 'Forwarded'):
                    for value in ('203.0.113.9', ''):
                        with self.subTest(peer=peer, header=name, empty=not value):
                            fields = {name: value}
                            code, headers, raw, _ = raw_request(self.s, '/health', peer=peer,
                                headers=fields, handler=self.s.Handler)
                            self.assertEqual(code, 200)
                            self.assertEqual(json.loads(raw), {'ok': True})
                            self.assertEqual(headers['Cache-Control'], 'no-store')
                            fields['Authorization'] = 'Bearer ' + token
                            code, _, raw, _ = raw_request(self.s, '/health', peer=peer,
                                headers=fields, handler=self.s.Handler)
                            self.assertEqual(code, 200)
                            self.assertIn('checks', json.loads(raw))

    def test_detail_local_or_authenticated_only(self):
        for peer in ('127.0.0.1', '172.18.0.5'):
            code, headers, raw, _ = raw_request(self.s, '/health', peer=peer, handler=self.s.Handler)
            self.assertEqual(code, 200)
            self.assertEqual('checks' in json.loads(raw), peer == '127.0.0.1')
        token = self.s.new_client('health-test', ['read'])
        code, headers, raw, _ = raw_request(self.s, '/health', peer='172.18.0.5', headers={'Authorization': 'Bearer '+token}, handler=self.s.Handler)
        self.assertEqual(code, 200)
        self.assertIn('checks', json.loads(raw))
        self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_server_observations_and_worker_start_once(self):
        with patch.object(self.s.threading, 'Thread') as thread:
            self.s.start_feed_worker()
            thread.return_value.is_alive.return_value = True
            self.s.start_feed_worker()
            self.assertEqual(thread.call_count, 1)
        self.assertIn('worker_alive', self.s.health_snapshot())

    def test_empty_row_has_one_safe_feed_rebuild(self):
        from skald.health import key
        cid, lang = self.s.rid('top10'), self.s.LANG
        row = key('row', (cid, 'movie', lang, None, None))
        self.s._health_row_sources[row] = cid
        result = self.s.health_actions()[row]()
        self.assertEqual(result, 'pending')
        self.assertEqual(self.s._health_jobs, {'top10'})

    def test_budget_over_recovers_on_new_day(self):
        self.s._usage_roll()
        self.s._decide_day = 'old-day'
        self.s._decide_over.add('fake-decision')
        self.assertFalse(self.s.health_snapshot()['budget_over'])

    def test_daily_lane_budget_near_cap(self):
        self.s._usage_roll()
        self.s._usage['calls'] = {'search:fake': self.s.AI_CAPS['search'] * .95}
        self.assertGreaterEqual(self.s.health_snapshot()['budget_ratio'], .9)

    def test_documentary_feed_stays_fresh_for_daily_schedule(self):
        cid, lang, now = self.s.DOCS[0], self.s.LANG, 200000
        self.s._feeds[cid, lang] = {'built': now - 3901, 'metas': {}}
        check = health.key('feed', ('docs', cid, lang))
        data = Path(self.temp.name)
        (data / 'health-settings.json').write_text(json.dumps({'enabled': True, 'repair': True}))
        h = health.Health(data, clock=lambda: now, transport=Mock())
        repair = Mock(return_value=False)
        observation = self.s.health_snapshot()
        observation.update(worker_alive=True, disk_free=1024**3)
        result = h.tick(observation, {check: repair})
        doc = next(c for c in result['checks'] if c['id'] == check)
        self.assertEqual(doc['interval'], 86400)
        self.assertEqual(doc['state'], 'ok')
        repair.assert_not_called()
        now += 86400
        result = h.tick(observation, {check: repair})
        self.assertEqual(next(c for c in result['checks'] if c['id'] == check)['state'], 'error')
        repair.assert_called_once()

    def test_connector_retry_keeps_persisted_backoff_after_restart(self):
        self.s._connectors['fake'] = {'next_due': 2000}
        self.s._health_poll_backoff.clear()
        with patch.object(self.s.time, 'time', return_value=1000):
            self.s.health_actions()[health.key('connector', 'fake')]()
        self.assertEqual(self.s._connectors['fake']['next_due'], 2000)

    def test_connector_retry_respects_backoff(self):
        from skald.health import key
        self.s._connectors['fake'] = {'next_due': 1000000}
        with patch.object(self.s.time, 'time', return_value=1000):
            self.s.health_actions()[key('connector', 'fake')]()
        self.assertEqual(self.s._connectors['fake']['next_due'], 1000000)
        self.s._connectors['fake']['next_due'] = 0
        self.s._health_poll_backoff['fake'] = 2000
        with patch.object(self.s.time, 'time', return_value=1000):
            self.s.health_actions()[key('connector', 'fake')]()
        self.assertEqual(self.s._connectors['fake']['next_due'], 2000)

    def test_force_stale_rebuild_does_not_skip_matching_stamp(self):
        cid, lang = self.s.rid('top10'), self.s.LANG
        self.s._feeds[cid, lang] = {'built': 1, 'stamp': 'v'+str(self.s.STAMP_V)+'/same', 'metas': {}}
        builder = Mock(return_value={cid: {'movie': []}})
        with patch.object(self.s, 'job_specs', return_value=[('top10', [cid], 'same', 0, builder)]), patch.object(self.s, 'connectors_tick'), patch.object(self.s, 'pin_decision'):
            self.s.build_feeds(self.s.local_now(), {}, {}, only={'top10'}, force=True)
        builder.assert_called_once()

    def test_aio_saved_setup_retry_is_private_and_reuses_logins(self):
        from skald.aio_refresh import BackgroundRefresh
        from skald import setup
        data = Path(self.temp.name)
        setup.write_private(data / 'setup.json', json.dumps({'aiostreams_pending': True, 'household_member': 'household', 'aio_url': 'http://aio.invalid'}))
        setup.write_private(data / 'credentials/aiostreams.json', json.dumps({'household': {'uuid': 'fake', 'password': 'FAKE-PASSWORD'}}))
        refresh = BackgroundRefresh(data, Mock())
        def sync(s, args, saved, ask, lines, show, **kwargs):
            self.assertEqual(ask(saved['aio_url'], 'address', None, '--aio-url'), saved['aio_url'])
            self.assertTrue(kwargs['private'])
            return {'ok': True}
        with patch('skald.setup.server') as server, patch('skald.setup.setup_aiostreams', side_effect=sync) as call:
            server.return_value.__enter__.return_value = self.s
            refresh.retry()
        call.assert_called_once()
        self.assertFalse(refresh.failed)
        self.assertEqual(json.loads((data / 'credentials/aiostreams.json').read_text())['household']['password'], 'FAKE-PASSWORD')

    def test_serving_observation_covers_shared_function(self):
        from skald.health import key
        self.s._health = Mock()
        cid, typ, lang = self.s.rid('top10'), 'movie', self.s.LANG
        with patch.object(self.s, '_row_metas_current', return_value=[{'id': 'tmdb:1'}]):
            self.s.row_metas(cid, typ, lang)
        check = key('row', (cid, typ, lang, None, None))
        self.assertEqual(self.s._health_served_rows[check], 1)
        with patch.object(self.s, '_row_metas_current', return_value=[]):
            self.s.row_metas(cid, typ, lang)
        self.assertEqual(self.s._health_served_rows[check], 0)

    def test_account_errors_count_as_connector_failure_without_stored_shape_changes(self):
        from skald.health import key, Health
        self.s._health = Health(self.temp.name)
        self.s.health_poll_result('fake', True)
        self.s.health_poll_result('fake', True)
        self.s.health_poll_result('fake', False)
        self.s.health_poll_result('fake', True)
        self.assertEqual(self.s._health_poll_runs['fake'], 1)
        self.s._health.state['polls'][key('connector', 'restored')] = {'failures': 2}
        self.s.health_poll_result('restored', True)
        self.assertEqual(self.s._health_poll_runs['restored'], 3)

    def test_aio_repair_drains_refresh_requested_during_repair(self):
        from skald.aio_refresh import BackgroundRefresh
        from skald import setup
        data = Path(self.temp.name)
        setup.write_private(data / 'setup.json', '{}')
        setup.write_private(data / 'credentials/aiostreams.json', '{"household": {"uuid": "u", "password": "p"}}')
        background = BackgroundRefresh(data, Mock())
        background.thread = Mock()  # schedule sees the running repair
        background.pending = True
        def refreshed(*args, **kwargs):
            if transport.call_count == 1:
                background.schedule()
            return 'AIOStreams rows refreshed (1 logins)'
        with patch('skald.aio_refresh.refresh', side_effect=refreshed) as transport, patch('skald.aio_refresh.time.sleep'):
            background.retry()
        self.assertEqual(transport.call_count, 2)
        self.assertFalse(background.pending)
        self.assertIsNone(background.thread)
        self.assertFalse(background.failed)

    def test_cache_repair_replaces_loaded_records_and_serving_caches(self):
        data, cid, lang = Path(self.temp.name), self.s.rid('top10'), self.s.LANG
        good = {cid + '|' + lang: {'stamp': 'current', 'built': 1,
                                  'metas': {'movie': [{'id': 'tmdb:1', 'type': 'movie'}]}}}
        (data / 'health-settings.json').write_text(json.dumps({'enabled': True, 'repair': True}))
        (data / 'health-cache-feeds.json').write_text(json.dumps(good))
        broken = {cid + '|' + lang: {'stamp': 'current', 'built': 1, 'metas': 'broken'}}
        (data / 'feeds.json').write_text(json.dumps(broken))
        with patch.object(self.s, 'connectors_load'), patch.object(self.s, 'load_emb'), patch.object(self.s, 'load_facets'), patch.object(self.s, 'load_decisions'):
            self.s.load_state()
        self.assertEqual(self.s._feeds[cid, lang]['metas'], 'broken')
        self.s._home_cache['old'] = 'old'
        self.s._pin_cache['old'] = 'old'
        h = health.Health(data, cache_changed=lambda name, records: self.s.health_cache_changed(name, records))
        h.tick({'worker_alive': True, 'disk_free': 1024**3})
        self.assertEqual(self.s.row_list(cid, 'movie', lang), good[cid + '|' + lang]['metas']['movie'])
        self.assertFalse(self.s._home_cache)
        self.assertFalse(self.s._pin_cache)
        self.assertEqual(json.loads((data / 'feeds.json').read_text()), good)
        result = h.tick({'worker_alive': True, 'disk_free': 1024**3})
        self.assertEqual(next(c for c in result['checks'] if c['id'] == 'json:feeds.json')['state'], 'ok')

    def test_aio_failed_refresh_remains_pending(self):
        from skald.aio_refresh import BackgroundRefresh
        refresh = BackgroundRefresh(self.temp.name, Mock())
        with patch('skald.aio_refresh.time.sleep'), patch('skald.aio_refresh.refresh', return_value='AIOStreams refresh failed; check its settings'):
            refresh.run()
        self.assertTrue(refresh.failed)
