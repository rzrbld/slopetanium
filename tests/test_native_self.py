#!/usr/bin/env python3
"""Writable system app updates use original ZIPs and preserve external settings."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'port/slopetanium/app'))
import apps
import core
from test_core import wait

class NativeSelfUpdate(unittest.TestCase):
    def test_independent_update_rollback_and_external_settings_without_portmaster(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); packages=root/'manager/packages'; entries=root/'manager/.entries'; conf=root/'manager/conf'
            first=root/'first.zip'; second=root/'second.zip'
            for path,version in [(first,'v1'),(second,'v2')]:
                with zipfile.ZipFile(path,'w') as z:
                    z.writestr('Slopetanium.sh','#!/bin/bash\ntrue\n')
                    z.writestr('slopetanium/app/native-app.json',json.dumps({'schema':1}))
                    z.writestr('slopetanium/port.json',json.dumps({'name':'slopetanium.zip'}))
                    z.writestr('slopetanium/app/core.py',version)
                    z.writestr('slopetanium/slopetanium-ui.aarch64',b'\x7fELF'+version.encode())
            info=apps.Apps(str(packages),str(entries)).install('rzrbld/slopetanium','v1',str(first)); install=Path(info['_dir'])
            conf.mkdir(); (conf/'preferences.ini').write_text('keep my choices')
            env={'DEVICE_ARCH':'aarch64','SLOP_SELF_APPS_DIR':str(packages),'SLOP_SELF_SHORTCUTS_DIR':str(entries),'SLOP_CONF_DIR':str(conf)}
            class Http:
                def get_json(self,url,etag=None):
                    return 200,[{'tag_name':tag,'published_at':'2026-11-'+day,'assets':[{'name':'slopetanium.zip','browser_download_url':str(path),'size':path.stat().st_size}]} for tag,path,day in [('v2',second,'23'),('v1',first,'22')]],None,None
                def download(self,url,dest,progress=None): shutil.copyfile(url,dest)
            with mock.patch.dict(os.environ,env):
                c=core.Core(str(install),'',ports_dir=str(root/'ports'),harbourmaster='/must-not-run',catalogue=False,apps_dir=str(root/'apps'),app_shortcuts_dir=str(root/'tiles'))
                try:
                    c.http=Http(); c.community.count_install=lambda *args:None
                    legacy=root/'legacy.zip'
                    with zipfile.ZipFile(legacy,'w') as z: z.writestr('Slopetanium.sh','old release')
                    with self.assertRaises(RuntimeError): c._check_zip(str(legacy),{'repo':'rzrbld/slopetanium'})
                    r=c.add_repo({'repo':'rzrbld/slopetanium'}); wait(c)
                    with mock.patch.object(c,'_make_room_for_self_update') as rename, mock.patch.object(c,'_refuse_if_running') as running:
                        job=c.queue_install(r['id'],'v2',force=True); wait(c); self.assertEqual(job.state,'done',job.message)
                        rename.assert_not_called(); running.assert_not_called()
                    self.assertEqual((install/'slopetanium/app/core.py').read_text(),'v2')
                    self.assertEqual((conf/'preferences.ini').read_text(),'keep my choices')
                    self.assertFalse((root/'ports/Slopetanium.sh').exists()); self.assertFalse((root/'tiles/Slopetanium.sh').exists())
                    self.assertEqual(c.conf_dir,str(conf)); self.assertEqual(c.port_dir(r),str(install))
                    job=c.queue_install(r['id'],'v1',force=True); wait(c); self.assertEqual(job.state,'done',job.message)
                    self.assertEqual((install/'slopetanium/app/core.py').read_text(),'v1')
                    self.assertEqual((conf/'preferences.ini').read_text(),'keep my choices')
                    job=c.queue_uninstall(r['id']); wait(c); self.assertEqual(job.state,'failed')
                    self.assertTrue(install.exists())
                finally: c.stop()
if __name__=='__main__': unittest.main()
