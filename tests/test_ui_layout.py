#!/usr/bin/env python3
"""Render the real UI against a local, static API; no GitHub or app installs."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import struct
import subprocess
import tempfile
import threading

CASES = [
    ('legacy','640x480','640x480',2,1),
    ('duo','1920x1080','1280x960',2,2),
    ('wide-bottom','640x480','1280x720',2,1),
    ('single-wide','1280x720','1280x720',1,1),
]
STATE = {'busy':False,'settings':{'count_installs':False},
         'web':{'running':True,'online':True,'pin':'1234','urls':['http://192.168.1.2:8686']},
         'repos':[{'id':'fixture','repo':'kyannon/gba-ds','title':'A Linux app with a longer title',
                   'status':'update','installed':True,'installed_package_type':'app','installed_tag':'v1','latest_tag':'v2',
                   'size':6910255,'releases':[{'tag':'v2','name':'v2','body':'Release notes use the full width of this panel. '*12}],
                   'keep_on_uninstall':['conf'],'originals':{'files':[],'total':0}}]}

class Api(BaseHTTPRequestHandler):
    def do_GET(self):
        value={'rows':['11111','10001','10101','10001','11111']} if self.path.startswith('/api/qr') else STATE
        self.reply(value)
    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length','0')))
        self.reply({'ok':True,'urls':['http://192.168.1.2:8686']})
    def reply(self,value):
        body=json.dumps(value).encode()
        self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

def size(text): return tuple(map(int,text.split('x')))

def check_images(output):
    from PIL import Image
    for name,top,bottom,screens,scale in CASES:
        height=int(480*scale)
        for view in ('shelf','menu','connect'):
            for screen,dimensions in (('top',top),('bottom',bottom)):
                image=Image.open(output/(name+'-'+view+'-'+screen+'.png')).convert('RGB')
                w,h=size(dimensions); expected=(int(math.floor(w/h*height+0.5)),height)
                assert image.size==expected,(name,view,screen,image.size,expected)
                # A left-over 4:3 buffer/stride or clipped background leaves the
                # added right portion black. Check within the panel, not its frame.
                x=int(image.width*0.92); y=int(image.height*0.48)
                assert image.getpixel((x,y))!=(0,0,0),(name,view,screen,'unpainted right side')
                if view=='shelf' and screen=='bottom':
                    left=image.getpixel((int(67*scale),int(100*scale)))
                    right=image.getpixel((image.width-int(68*scale),int(100*scale)))
                    assert sum(left)>280 and sum(right)>280,(name,'shelf bubble did not widen with its text')
                    xs=[]
                    for yy in range(int(75*scale),int(125*scale)):
                        for xx in range(image.width):
                            r,g,b=image.getpixel((xx,yy))
                            if min(r,g,b)>200: xs.append(xx)
                    assert xs,(name,'missing shelf title')
                    assert abs((min(xs)+max(xs)+1)/2-image.width/2)<=4*scale,(name,'off-centre shelf title')
    print('UI layouts: equal panels, Duo Lite, wide lower panel and single widescreen passed')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--binary',default='ui/slopetanium-ui'); ap.add_argument('--assets',default='port/slopetanium/assets')
    ap.add_argument('--output',required=True); ap.add_argument('--render-only',action='store_true'); args=ap.parse_args()
    binary=str(Path(args.binary).resolve()); assets=str(Path(args.assets).resolve()); output=Path(args.output).resolve(); output.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer(('127.0.0.1',0),Api); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='slop-ui-') as tmp:
            port=Path(tmp)/'core.port'; port.write_text(str(server.server_address[1]))
            for name,top,bottom,screens,scale in CASES:
                for view,script in [('shelf',''),('menu','a'),('connect','right,a')]:
                    prefix=output/(name+'-'+view)
                    command=[binary,'--assets',assets,'--conf',tmp,'--port-file',str(port),'--token','layout-test',
                             '--screens',str(screens),'--top-size',top,'--bottom-size',bottom,'--scale',str(scale),'--shot',str(prefix),'--script',script]
                    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=45)
                    (output/(name+'-'+view+'.log')).write_bytes(result.stdout)
                    if result.returncode: raise RuntimeError(result.stdout.decode(errors='replace'))
                    for side in ('top','bottom'):
                        data=Path(str(prefix)+'-'+side+'.png').read_bytes()
                        assert data[:8]==b'\x89PNG\r\n\x1a\n'
                        print(name,view,side,struct.unpack('>II',data[16:24]))
    finally: server.shutdown(); server.server_close(); thread.join()
    if not args.render_only: check_images(output)

if __name__=='__main__': main()
