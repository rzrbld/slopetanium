#!/usr/bin/env python3
"""Original-release app installation, updates, rollback, launchers and isolation."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'port/slopetanium/app'))
import apps
import core
from test_core import wait


def package(path, version, extra=None):
    # Same layout as kyannon/gba-ds: release wrapper + app folder, no port.json.
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('GBA DS.sh', '#!/bin/bash\nexec bash "$(dirname "$0")/gba-ds/launch.sh" "$@"\n')
        z.writestr('gba-ds/launch.sh', '#!/bin/bash\nprintf "%s\\n" "'+version+'" "$@"\n')
        z.writestr('gba-ds/gba-ds', b'\x7fELF'+version.encode())
        z.writestr('gba-ds/gba-ds.cfg', 'volume='+version+'\n')
        for name,data in (extra or {}).items(): z.writestr(name,data)


class AppInstall(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.base=Path(self.tmp.name)
        self.manager=apps.Apps(str(self.base/'apps'),str(self.base/'tiles'))
        self.v1=self.base/'v1.zip'; self.v2=self.base/'v2.zip'
        package(self.v1,'v1',{'gba-ds/obsolete.bin':'old'})
        package(self.v2,'v2')
        self.repo='kyannon/gba-ds'
    def tearDown(self): self.tmp.cleanup()
    def install(self,archive,tag): return self.manager.install(self.repo,tag,str(archive))
    def test_install_update_rollback_and_uninstall_without_harbourmaster(self):
        info=self.install(self.v1,'v1'); root=Path(info['_dir']); shortcut=Path(info['items'][0])
        self.assertTrue((root/'gba-ds/gba-ds').stat().st_mode & stat.S_IXUSR)
        result=subprocess.check_output(['bash',str(shortcut),'an argument with spaces'],text=True)
        self.assertEqual(result,'v1\nan argument with spaces\n')
        self.install(self.v2,'v2')
        self.assertEqual((root/'gba-ds/gba-ds.cfg').read_text(),'volume=v2\n')
        self.assertFalse((root/'gba-ds/obsolete.bin').exists())
        (root/'gba-ds/gba-ds.cfg').write_text('my preferences\n')
        (root/'gba-ds/states').mkdir(); (root/'gba-ds/states/game.sav').write_bytes(b'user-save')
        self.install(self.v1,'v1')
        self.assertEqual((root/'gba-ds/gba-ds.cfg').read_text(),'my preferences\n')
        self.assertEqual((root/'gba-ds/states/game.sav').read_bytes(),b'user-save')
        self.assertEqual(self.manager.info(self.repo)['_app']['tag'],'v1')
        self.manager.uninstall(self.repo)
        self.assertFalse(root.exists()); self.assertFalse(shortcut.exists())
    def test_shortcut_collision_does_not_replace_existing_app(self):
        (self.base/'tiles').mkdir(); (self.base/'tiles/GBA DS.sh').write_text('another app')
        with self.assertRaises(ValueError): self.install(self.v1,'v1')
        self.assertEqual((self.base/'tiles/GBA DS.sh').read_text(),'another app')
        self.assertIsNone(self.manager.info(self.repo))
    def test_failed_publish_restores_previous_payload_and_launcher(self):
        info=self.install(self.v1,'v1'); root=Path(info['_dir']); link=Path(info['items'][0]); before=link.read_bytes()
        with mock.patch.object(apps.os,'replace',side_effect=OSError('simulated write failure')):
            with self.assertRaises(OSError): self.install(self.v2,'v2')
        self.assertEqual(self.manager.info(self.repo)['_app']['tag'],'v1')
        self.assertEqual((root/'gba-ds/gba-ds').read_bytes(),b'\x7fELFv1')
        self.assertEqual(link.read_bytes(),before)
    def test_interrupted_directory_swap_recovers(self):
        info=self.install(self.v1,'v1'); root=Path(info['_dir']); root.rename(str(root)+'.previous')
        self.install(self.v2,'v2')
        self.assertEqual(self.manager.info(self.repo)['_app']['tag'],'v2')
    def test_reject_unsafe_archives_and_existing_symlink_parents(self):
        for bad in ('../outside','/absolute','a/../../outside','a\\outside',apps.MARKER):
            evil=self.base/'evil.zip'; package(evil,'bad',{bad:'bad'})
            with self.subTest(path=bad), self.assertRaises(ValueError): self.install(evil,'bad')
        evil=self.base/'link.zip'
        with zipfile.ZipFile(evil,'w') as z:
            z.writestr('Launch.sh','#!/bin/bash\ntrue')
            item=zipfile.ZipInfo('symlink'); item.create_system=3; item.external_attr=(stat.S_IFLNK|0o777)<<16
            z.writestr(item,'/tmp')
        with self.assertRaises(ValueError): self.install(evil,'bad')
        info=self.install(self.v1,'v1'); root=Path(info['_dir'])
        outside=self.base/'outside'; outside.mkdir(); (outside/'untouched').write_text('safe')
        (root/'escape').symlink_to(outside,target_is_directory=True)
        evil=self.base/'parent.zip'; package(evil,'bad',{'escape/new/file':'bad'})
        with self.assertRaises(ValueError): self.install(evil,'bad')
        self.assertFalse((outside/'new').exists())
        self.assertEqual((outside/'untouched').read_text(),'safe')
    def test_reject_cpu_mismatch_and_keep_neutral_archives(self):
        self.assertTrue(apps.compatible_asset("app-linux-aarch64.zip", "arm64"))
        self.assertTrue(apps.compatible_asset("app.zip", "armv7l"))
        self.assertFalse(apps.compatible_asset("app-linux-amd64.zip", "aarch64"))
        wrong=self.base/"app-linux-aarch64.zip"; shutil.copyfile(self.v1,wrong)
        manager=apps.Apps(str(self.base/'apps'),str(self.base/'tiles'),target_arch="armv7l")
        with self.assertRaises(ValueError): manager.install(self.repo,'v1',str(wrong))

    def test_keep_modified_config_but_replace_code_with_exact_upstream_bytes(self):
        info=self.install(self.v1,'v1'); root=Path(info['_dir'])
        (root/'gba-ds/gba-ds.cfg').write_text('my volume')
        (root/'gba-ds/gba-ds').write_bytes(b'changed by app')
        self.install(self.v2,'v2')
        with zipfile.ZipFile(self.v2) as z:
            self.assertEqual((root/'gba-ds/gba-ds').read_bytes(),z.read('gba-ds/gba-ds'))
        self.assertEqual((root/'gba-ds/gba-ds.cfg').read_text(),'my volume')


class CoreApps(unittest.TestCase):
    def test_add_original_repo_install_refresh_rollback_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"DEVICE_ARCH":"aarch64"}):
            root=Path(tmp); first=root/'v1.zip'; second=root/'v2.zip'; package(first,'v1'); package(second,'v2')
            class Http:
                def get_json(self,url,etag=None):
                    return 200,[{'tag_name':tag,'published_at':'2026-11-'+day,'assets':[
                        {'name':'gba-ds-'+tag+'-linux-aarch64.zip','browser_download_url':str(path),'size':path.stat().st_size}]} for tag,path,day in [('v2',second,'22'),('v1',first,'21')]],None,None
                def download(self,url,dest,progress=None): shutil.copyfile(url,dest)
            c=core.Core(str(root/'ports/slopetanium'),'',ports_dir=str(root/'ports'),harbourmaster='/must-not-run',catalogue=False,
                        apps_dir=str(root/'apps'),app_shortcuts_dir=str(root/'tiles'))
            try:
                c.http=Http(); c.community.count_install=lambda *args:None
                r=c.add_repo({'repo':'https://github.com/kyannon/gba-ds'}); wait(c)
                job=c.queue_install(r['id'],force=True); wait(c); self.assertEqual(job.state,'done',job.message)
                self.assertTrue(c.repo_view(r)['installed']); self.assertEqual(r['installed_package_type'],'app')
                c.check_repo(r['id']); self.assertEqual(r['installed_tag'],'v2')
                job=c.queue_install(r['id'],'v1',force=True); wait(c); self.assertEqual(job.state,'done',job.message)
                self.assertEqual(r['installed_tag'],'v1'); self.assertTrue(c.has_update(r))
                job=c.queue_uninstall(r['id'],backup=True); wait(c); self.assertEqual(job.state,'done',job.message)
                self.assertFalse(c.repo_view(r)['installed'])
            finally: c.stop()

if __name__=='__main__': unittest.main()
