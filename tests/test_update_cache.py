#!/usr/bin/env python3
"""Persistent automatic-check TTLs; manual refresh remains unconditional."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'port/slopetanium/app'))
import core

class Http:
    def __init__(self): self.calls = []; self.fail = False; self.empty = False
    def get_json(self, url, etag=None):
        self.calls.append((url, etag))
        if self.fail: raise OSError('offline')
        if etag: return 304, None, etag, '59'
        releases = [] if self.empty else [{'tag_name':'v1','published_at':'2026-11-01T00:00:00Z',
                     'assets':[{'name':'demo.zip','browser_download_url':'https://example/demo.zip'}]}]
        return 200, releases, 'etag1', '59'

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = patch.object(core, 'now', return_value=10000).start()
        # Control the real Core queue deterministically; never start networking
        # background tasks or wait an hour in a scheduler regression test.
        self.threads = patch.object(core.threading, 'Thread').start()
        self.timer = patch.object(core.threading, 'Timer').start()
        self.http = Http()
        self.c = self.open_core()
    def tearDown(self):
        patch.stopall(); self.tmp.cleanup()
    def open_core(self):
        c = core.Core(self.tmp.name, '', catalogue=False, harbourmaster='/bin/false')
        c.http = self.http
        return c
    def add(self, name='demo'):
        r = {'id':name, 'repo':'owner/'+name, 'package_type':'app'}
        self.c._repo_defaults(r); self.c.state['repos'].append(r); self.c.save()
        return r
    def drain(self, c=None):
        c = c or self.c
        while not c.job_queue.empty():
            j = c.job_queue.get_nowait(); j.state='running'; j.fn(j); j.state='done'
    def test_reopen_reuses_persisted_release_and_status_for_one_hour(self):
        r=self.add(); self.c.queue_check(); self.drain()
        self.clock.return_value += 60
        reopened=self.open_core()
        self.assertEqual(reopened.state['repos'][0]['releases'],r['releases'])
        self.assertEqual(reopened.queue_check(max_age=3600),[])
        self.assertEqual(len(self.http.calls),1)
        self.clock.return_value = 13600
        self.assertEqual(len(reopened.queue_check(max_age=3600)),1)
        self.drain(reopened)
        self.assertEqual(self.http.calls[-1][1],'etag1')
        self.assertEqual(len(self.http.calls),2)
    def test_only_stale_repositories_are_checked(self):
        fresh=self.add('fresh'); stale=self.add('stale')
        fresh.update(last_checked=9990,last_check_succeeded=True)
        stale.update(last_checked=6000,last_check_succeeded=True)
        self.assertEqual([j.repo_id for j in self.c.queue_check(max_age=3600)],['stale'])
    def test_manual_refresh_bypasses_cache_and_304_refreshes_its_age(self):
        r=self.add(); self.c.queue_check(); self.drain()
        self.clock.return_value += 1
        self.assertEqual(len(self.c.queue_check(r['id'])),1); self.drain()
        self.assertEqual(len(self.http.calls),2)
        self.assertEqual(r['last_checked'],10001)
        self.assertEqual(self.c.queue_check(max_age=3600),[])
    def test_failed_checks_retry_after_one_minute_not_one_hour(self):
        r=self.add(); self.http.fail=True; self.c.queue_check(); self.drain()
        self.assertFalse(r['last_check_succeeded'])
        self.clock.return_value += 59
        self.assertEqual(self.c.queue_check(max_age=3600),[])
        self.clock.return_value += 1
        self.assertEqual(len(self.c.queue_check(max_age=3600)),1)
    def test_empty_successful_feed_is_cached_too(self):
        r=self.add(); self.http.empty=True; self.c.queue_check(); self.drain()
        self.assertEqual(r['error'],'No releases yet')
        self.assertTrue(r['last_check_succeeded'])
        self.clock.return_value += 61
        self.assertEqual(self.c.queue_check(max_age=3600),[])
    def test_manual_requests_reuse_an_already_pending_check(self):
        r=self.add(); a=self.c.queue_check(r['id']); b=self.c.queue_check(r['id'])
        self.assertIs(a[0],b[0]); self.assertEqual(self.c.job_queue.qsize(),1)
    def test_clock_rollback_does_not_cache_indefinitely(self):
        r=self.add(); r.update(last_checked=11000,last_check_succeeded=True)
        self.assertEqual(len(self.c.queue_check(max_age=3600)),1)
    def test_old_saved_successful_checks_migrate_without_fetching(self):
        r=self.add(); r.update(last_checked=9999,error=None); del r['last_check_succeeded']; self.c.save()
        reopened=self.open_core()
        self.assertTrue(reopened.state['repos'][0]['last_check_succeeded'])
        self.assertEqual(reopened.queue_check(max_age=3600),[])
    def test_scheduler_uses_cached_startup_and_periodic_checks(self):
        calls=[]
        class Stop:
            def __init__(self): self.waits=0
            def wait(self,seconds):
                self.waits+=1
                return self.waits==3
        self.c._stop=Stop()
        with patch.object(self.c,'queue_check',side_effect=lambda **kw:calls.append(kw)):
            self.c._auto_checker()
        self.assertEqual(calls,[{'max_age':3600},{'max_age':3600}])
    def test_zero_interval_keeps_cached_startup_but_no_periodic_check(self):
        self.c.state['settings']['auto_check_minutes']=0
        calls=[]
        class Stop:
            def __init__(self): self.waits=0
            def wait(self,seconds): self.waits+=1; return self.waits==3
        self.c._stop=Stop()
        with patch.object(self.c,'queue_check',side_effect=lambda **kw:calls.append(kw)):
            self.c._auto_checker()
        self.assertEqual(calls,[{'max_age':3600}])

if __name__=='__main__': unittest.main()
